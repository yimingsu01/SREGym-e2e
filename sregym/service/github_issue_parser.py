"""Resolve a GitHub issue URL to the buggy database version and git ref.

Given an issue URL the parser:
  1. Identifies which DBBuildSpec to use (via the repo's github_repo field)
  2. Finds the buggy commit/ref using this fallback chain:
       a. Specific commit SHA in the issue body or timeline
       b. Version label  (e.g. "4.1.7" or "affects/4.1.7")
       c. Milestone version
       d. Semver found in issue title or body
  3. Returns a ParsedIssue with everything needed to build a problem

Auth: set GITHUB_TOKEN in the environment for authenticated requests
(5000 req/hr vs 60 req/hr unauthenticated).
"""

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from sregym.service.db_build_spec import DB_REGISTRY, DBBuildSpec
from sregym.service.reproducer_extractor import extract_reproducer_full

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"

# Matches a full 40-char SHA or an abbreviated 7–12 char one that looks
# intentional (preceded by word boundary, slash, @, or "commit").
_SHA_RE = re.compile(
    r"(?:commit[/ ]|SHA:?\s*|@)([0-9a-f]{7,40})\b"
    r"|(?<!\w)([0-9a-f]{40})(?!\w)",
    re.IGNORECASE,
)

# Matches bare semver-ish strings: 4.1.7, 3.0, 1.2.3.4, etc.
_VERSION_RE = re.compile(r"\b(\d+\.\d+(?:\.\d+)*)\b")

# Label patterns that carry a version: "4.1.7", "affects/4.1.7",
# "affects-version/4.1.7", "version: 4.1.7", "affects-versions/4.1.7"
_VERSION_LABEL_RE = re.compile(
    r"(?:affects?[-/]?versions?[-/:]?\s*)?(\d+\.\d+(?:\.\d+)*)",
    re.IGNORECASE,
)


@dataclass
class ParsedIssue:
    spec: DBBuildSpec
    # Bare version string, e.g. "4.1.7"
    version: str
    # Git ref to clone — a tag or commit SHA
    git_ref: str
    # Whether git_ref is a specific commit SHA (True) or a version tag (False)
    is_commit: bool
    # Raw issue metadata for downstream use
    title: str
    body: str
    # Reproducer script/query extracted from the issue body (None if not found)
    reproducer: str | None = None
    # For wrong-result bugs: the value the readiness probe greps for.
    expected_output: str | None = None
    # True when the bug causes the process to crash on startup (not at runtime).
    # setup_preconditions() in the generated file must be filled in manually.
    crash_on_startup: bool = False
    # Verbatim output the description claims the reproducer produces on a BUGGY
    # binary. Used by reproducer_validator to confirm the reproducer triggers
    # the bug; does NOT flow into the generated problem file.
    buggy_output: str | None = None
    # Verbatim output the description claims the reproducer produces on a FIXED
    # binary. Used by reproducer_validator alongside buggy_output.
    correct_output: str | None = None


class GitHubIssueParser:
    def __init__(self, issue_url: str):
        self.issue_url = issue_url
        owner, repo, number = self._parse_url(issue_url)
        self.owner = owner
        self.repo = repo
        self.number = number
        self._token = os.environ.get("GITHUB_TOKEN", "")

    def resolve(self) -> ParsedIssue:
        spec = self._lookup_spec()
        issue = self._fetch(f"/repos/{self.owner}/{self.repo}/issues/{self.number}")
        body = issue.get("body") or ""
        text = (issue.get("title") or "") + "\n" + body

        git_ref, is_commit = self._find_ref(issue, text, spec)
        version = self._extract_version(text, issue, spec)
        reproducer, expected_output, buggy_output, correct_output, crash_on_startup = (
            extract_reproducer_full(body)
        )
        if reproducer or crash_on_startup:
            logger.info(
                f"[IssueParser] Extracted reproducer ({len(reproducer) if reproducer else 0} chars)"
                + (f", expected_output={expected_output!r}" if expected_output else "")
                + (f", buggy_output={buggy_output!r}" if buggy_output else "")
                + (f", correct_output={correct_output!r}" if correct_output else "")
                + (", crash_on_startup=True" if crash_on_startup else "")
            )

        logger.info(
            f"[IssueParser] {self.issue_url} → db={spec.name} "
            f"version={version} ref={git_ref} commit={is_commit}"
        )
        return ParsedIssue(
            spec=spec,
            version=version,
            git_ref=git_ref,
            is_commit=is_commit,
            title=issue.get("title", ""),
            body=body,
            reproducer=reproducer,
            expected_output=expected_output,
            crash_on_startup=crash_on_startup,
            buggy_output=buggy_output,
            correct_output=correct_output,
        )

    # ── Ref resolution (fallback chain) ──────────────────────────────────────

    def _sha_exists_in_repo(self, sha: str) -> bool:
        """Return True if sha is a valid commit in this repo."""
        try:
            self._fetch(f"/repos/{self.owner}/{self.repo}/commits/{sha}")
            return True
        except Exception:
            return False

    def _tag_exists_in_repo(self, tag: str) -> bool:
        """Return True if tag exists in this repo."""
        try:
            self._fetch(f"/repos/{self.owner}/{self.repo}/git/ref/tags/{tag}")
            return True
        except RuntimeError as e:
            if "404" not in str(e):
                logger.warning(f"[IssueParser] Tag check for {tag!r} unexpected error: {e}")
            return False
        except Exception as e:
            logger.warning(f"[IssueParser] Tag check for {tag!r} failed: {e}")
            return False

    def _find_ref(
        self, issue: dict, text: str, spec: DBBuildSpec
    ) -> tuple[str, bool]:
        # a. Specific commit SHA in body — validate it exists in this repo
        sha = self._find_sha_in_text(text)
        if sha:
            if self._sha_exists_in_repo(sha):
                logger.debug(f"[IssueParser] ref from body SHA: {sha}")
                return sha, True
            logger.debug(f"[IssueParser] SHA {sha[:12]} not in repo — skipping")

        # b. Commit SHA from timeline events (cross-references, commits)
        sha = self._find_sha_in_timeline()
        if sha:
            logger.debug(f"[IssueParser] ref from timeline SHA: {sha}")
            return sha, True

        # c. Version label
        version = self._find_version_in_labels(issue)
        if version:
            ref = spec.git_ref(version)
            logger.info(f"[IssueParser] trying label version {version} → {ref}")
            if self._tag_exists_in_repo(ref):
                return ref, False

        # d. Milestone version
        version = self._find_version_in_milestone(issue)
        if version:
            ref = spec.git_ref(version)
            logger.info(f"[IssueParser] trying milestone version {version} → {ref}")
            if self._tag_exists_in_repo(ref):
                return ref, False

        # e. Semver in title/body — validate tag exists before committing
        version = self._find_version_in_text(text)
        if version:
            ref = spec.git_ref(version)
            logger.info(f"[IssueParser] trying text version {version} → {ref}")
            if self._tag_exists_in_repo(ref):
                return ref, False

        # f. LLM fallback
        version = self._llm_extract_version(text, spec)
        if version:
            ref = spec.git_ref(version)
            logger.info(f"[IssueParser] trying LLM version {version} → {ref}")
            if self._tag_exists_in_repo(ref):
                return ref, False

        raise ValueError(
            f"Could not determine buggy ref from issue: {self.issue_url}\n"
            "Add a version label, milestone, or mention a version/commit in the body."
        )

    def _llm_extract_version(self, text: str, spec: DBBuildSpec) -> str | None:
        """Use LLM to extract the buggy version from the issue text."""
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return None
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=32,
                messages=[{
                    "role": "user",
                    "content": (
                        f"What version of {spec.name} contains the bug described in this issue?\n"
                        f"Reply with ONLY the version number (e.g. '7.5.1'), no 'v' prefix, no other text.\n\n"
                        f"---\n{text[:4000]}\n---"
                    ),
                }],
            )
            raw = message.content[0].text.strip()
            logger.info(f"[IssueParser] LLM raw version response: {raw!r}")
            m = re.search(r'(\d+\.\d+(?:\.\d+)*)', raw)
            if m:
                version = m.group(1)
                logger.info(f"[IssueParser] LLM extracted version: {version}")
                return version
        except Exception as e:
            logger.warning(f"[IssueParser] LLM version extraction failed: {e}")
        return None

    def _find_sha_in_text(self, text: str) -> str | None:
        m = _SHA_RE.search(text)
        if m:
            return m.group(1) or m.group(2)
        return None

    def _find_sha_in_timeline(self) -> str | None:
        try:
            events = self._fetch(
                f"/repos/{self.owner}/{self.repo}/issues/{self.number}/timeline",
                extra_headers={"Accept": "application/vnd.github.mockingbird-preview+json"},
            )
        except Exception:
            return None

        for event in events:
            event_type = event.get("event", "")
            # "committed" events on referenced commits
            if event_type == "committed":
                sha = event.get("sha") or event.get("oid")
                if sha:
                    return sha
            # "referenced" events carry a commit object
            if event_type == "referenced":
                commit = event.get("commit_id") or (
                    event.get("source", {}).get("issue", {}).get("pull_request", {})
                )
                if isinstance(commit, str) and len(commit) >= 7:
                    return commit
        return None

    # ── Version extraction ────────────────────────────────────────────────────

    def _extract_version(self, text: str, issue: dict, spec: DBBuildSpec | None = None) -> str:
        """Best-effort version extraction; used for display / image tagging."""
        v = (
            self._find_version_in_labels(issue)
            or self._find_version_in_milestone(issue)
            or self._find_version_in_text(text)
        )
        if v:
            return v
        if spec:
            v = self._llm_extract_version(text, spec)
            if v:
                return v
        logger.warning(f"[IssueParser] Could not extract version from {self.issue_url}")
        return "unknown"

    def _find_version_in_labels(self, issue: dict) -> str | None:
        for label in issue.get("labels", []):
            name = label.get("name", "")
            m = _VERSION_LABEL_RE.fullmatch(name.strip())
            if m:
                return m.group(1)
        return None

    def _find_version_in_milestone(self, issue: dict) -> str | None:
        milestone = issue.get("milestone")
        if not milestone:
            return None
        title = milestone.get("title", "")
        m = _VERSION_RE.search(title)
        return m.group(1) if m else None

    def _find_version_in_text(self, text: str) -> str | None:
        # 1. Prefer explicit "v{major}.{minor}.{patch}" tags (e.g. "v7.5.1")
        m = re.search(r"\bv(\d+\.\d+\.\d+)\b", text)
        if m:
            return m.group(1)
        # 2. Lines that look like "affects version X" or "version: X"
        for line in text.splitlines():
            if re.search(r"affect|version|fixed.in|reported", line, re.IGNORECASE):
                m = _VERSION_RE.search(line)
                if m:
                    return m.group(1)
        # 3. First semver-ish anywhere in the text
        m = _VERSION_RE.search(text)
        return m.group(1) if m else None

    # ── Spec lookup ───────────────────────────────────────────────────────────

    def _lookup_spec(self) -> DBBuildSpec:
        target = f"{self.owner}/{self.repo}".lower()
        for spec in DB_REGISTRY.values():
            if spec.github_repo.lower() == target:
                return spec
        raise ValueError(
            f"No DBBuildSpec registered for repo '{self.owner}/{self.repo}'.\n"
            f"Add an entry to DB_REGISTRY in db_build_spec.py."
        )

    # ── GitHub API ────────────────────────────────────────────────────────────

    def _fetch(self, path: str, extra_headers: dict | None = None) -> dict | list:
        url = f"{_GITHUB_API}{path}"
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if extra_headers:
            headers.update(extra_headers)

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise RuntimeError(f"GitHub API {url} → {e.code}: {body}") from e

    # ── URL parsing ───────────────────────────────────────────────────────────

    @staticmethod
    def _parse_url(url: str) -> tuple[str, str, str]:
        m = re.search(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)", url)
        if not m:
            raise ValueError(f"Not a GitHub issue URL: {url}")
        return m.group(1), m.group(2), m.group(3)
