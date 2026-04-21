"""Base class for problems that build and deploy a custom (buggy) database binary.

Two usage modes:

  Auto-generated (issue-driven)
  ──────────────────────────────
  Point at a GitHub issue.  The parser resolves the DB type, version, and buggy
  git ref automatically.  The source tree at that commit IS the buggy state —
  no patch files needed.

      class AutoCassandra20108(GenericCustomBuildProblem):
          db_name   = "cassandra"
          issue_url = "https://github.com/apache/cassandra/issues/20108"

  Hand-crafted (patch-driven)
  ────────────────────────────
  Supply a patch directory and explicit version.  Modified source files are
  overlaid on a clean clone before building.

      class MyCassandraBug(GenericCustomBuildProblem):
          db_name        = "cassandra"
          db_version     = "4.1.7"
          source_git_ref = "cassandra-4.1.7"
          patch_dir      = Path(__file__).parent / "patches" / "my_bug"
          root_cause_description = "..."

Problem lifecycle
─────────────────
  __init__   — parse issue (if issue-driven) → clone source → build image
  deploy()   — install operator + deploy STOCK cluster (called by conductor)
  inject_fault() — swap running cluster to the buggy image (rolling restart)
  recover_fault() — no-op; conductor tears down the cluster between problems
"""

import logging
import re
from pathlib import Path

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.generic_db_app import GenericDBApplication
from sregym.service.db_build_spec import DB_REGISTRY, DBBuildSpec
from sregym.service.generic_db_build_manager import GenericDBBuildManager
from sregym.service.issue_parser import parse_issue
from sregym.service.source_manager import SourceManager
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


def _nearest_released_version(spec, version: str) -> str:
    """Return version if its Docker image exists, otherwise the latest released version."""
    import subprocess as _sp
    candidate = spec.resolved_base_image(version)
    ok = _sp.run(f"docker manifest inspect {candidate}", shell=True, capture_output=True)
    if ok.returncode == 0:
        return version
    logger.warning(f"[GenericCustomBuild] {candidate!r} not on Docker Hub — querying for latest release")
    try:
        import urllib.request as _ur, json as _js
        url = f"https://registry.hub.docker.com/v2/repositories/pingcap/{spec.name}/tags?page_size=50&ordering=last_updated"
        with _ur.urlopen(url, timeout=10) as r:
            tags = [t["name"] for t in _js.loads(r.read())["results"]]
        stable = [t.lstrip("v") for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
        if stable:
            return stable[0]
    except Exception as e:
        logger.warning(f"[GenericCustomBuild] Docker Hub query failed: {e}")
    return version


def _version_from_source(source_path: Path, spec) -> str | None:
    """Extract version from known version files in the source tree."""
    candidates = [
        # TiDB: pkg/parser/mysql/const.go — TiDBReleaseVersion = "v8.4.0-..."
        source_path / "pkg" / "parser" / "mysql" / "const.go",
        # Cassandra: version files
        source_path / "build.xml",
        source_path / "pom.xml",
        # MongoDB: version file in buildscripts
        source_path / "buildscripts" / "mongo_version.py",
    ]
    for path in candidates:
        if not path.exists():
            continue
        text = path.read_text(errors="replace")
        m = re.search(r'[Rr]elease[Vv]ersion\s*=\s*"v?(\d+\.\d+\.\d+)', text)
        if m:
            return m.group(1)
        m = re.search(r'<version>(\d+\.\d+\.\d+)', text)
        if m:
            return m.group(1)
        # MongoDB: mongo_version.py — version = "7.0.5"
        m = re.search(r'^version\s*=\s*["\'](\d+\.\d+\.\d+)', text, re.MULTILINE)
        if m:
            return m.group(1)
    return None


class GenericCustomBuildProblem(Problem):
    """Deploy any database at a specific buggy version and swap the image at fault-inject time.

    Subclasses must set ``db_name`` and either ``issue_url`` (auto mode) or
    ``db_version`` + ``source_git_ref`` + ``patch_dir`` (hand-crafted mode).
    """

    # ── Required ─────────────────────────────────────────────────────────────
    db_name: str   # key into DB_REGISTRY, e.g. "cassandra"

    # ── Auto mode (issue-driven) ──────────────────────────────────────────────
    issue_url: str | None = None

    # ── Hand-crafted mode (patch-driven) ─────────────────────────────────────
    db_version: str | None = None        # e.g. "4.1.7"
    source_git_ref: str | None = None    # e.g. "cassandra-4.1.7"
    patch_dir: Path | None = None

    # ── Optional overrides ────────────────────────────────────────────────────
    # If not set in auto mode, populated from the issue title + body.
    root_cause_description: str = ""
    root_cause_file: str = "source"
    # Script/query to run after the buggy image is active to trigger the bug.
    # If not set, populated from the parsed issue body.
    reproducer: str | None = None
    # For wrong-result bugs: the correct value the query should return.
    # Drives the readiness probe — Not Ready when wrong result, Ready when fixed.
    expected_output: str | None = None
    continuous_reproducer: bool = False
    # Set True for bugs that crash the DB process on startup (not at query time).
    # inject_fault() will call setup_preconditions() first (to set up required
    # cluster state while the stock binary is still running), then swap to the
    # buggy binary and wait for a crash-loop rather than a Ready pod.
    crash_on_startup: bool = False

    # ── Init ─────────────────────────────────────────────────────────────────

    def __init__(self):
        if self.db_name not in DB_REGISTRY:
            raise ValueError(
                f"Unknown db_name '{self.db_name}'. "
                f"Add an entry to DB_REGISTRY in db_build_spec.py."
            )
        spec: DBBuildSpec = DB_REGISTRY[self.db_name]

        version, git_ref = self._resolve_version_and_ref(spec)
        source_path = SourceManager().ensure_source(
            repo_url=spec.repo_url,
            git_ref=git_ref,
            name=spec.name,
        )

        _major = int(version.split(".")[0]) if re.match(r'^\d', version) else 0
        if version == "unknown" or _major == 0:
            version = _version_from_source(source_path, spec) or version
            logger.info(f"[GenericCustomBuild] Resolved version from source tree: {version}")

        # For pre-release versions whose Docker image doesn't exist, use the nearest
        # released version for the stock cluster while the custom binary still comes
        # from the actual source commit.
        deploy_version = _nearest_released_version(spec, version)
        if deploy_version != version:
            logger.info(
                f"[GenericCustomBuild] {version!r} has no Docker image — "
                f"deploying stock cluster at {deploy_version!r}"
            )

        build_mgr = GenericDBBuildManager(spec, source_path, version)
        if self.patch_dir is not None:
            self._custom_image = build_mgr.build_with_patches(Path(self.patch_dir))
        else:
            self._custom_image = build_mgr.build_from_directory()

        logger.info(f"[GenericCustomBuild] Using image: {self._custom_image}")

        # crash_on_startup bugs need the stock cluster up to run setup_preconditions()
        # before the swap, so always deploy with the stock image for those.
        if self.crash_on_startup:
            self._predeployed_buggy = False
            initial_image = None
        else:
            self._predeployed_buggy = deploy_version != version
            initial_image = self._custom_image if self._predeployed_buggy else None
        app = GenericDBApplication(spec, deploy_version, initial_image=initial_image)
        super().__init__(app=app, namespace=app.namespace)

        self.source_code_path = source_path

        root_cause = self.build_structured_root_cause(
            component=f"source/{self.root_cause_file}",
            namespace=self.namespace,
            description=self.root_cause_description or f"Bug in {spec.name} {version}",
        )
        self.root_cause = root_cause
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=root_cause)
        self.mitigation_oracle = None

    # ── Fault injection ───────────────────────────────────────────────────────

    def setup_preconditions(self):
        """Override to prepare cluster state before the buggy image is swapped in.

        Called during inject_fault() while the stock binary is still running.
        Typical uses: enable a feature flag, add a DDL index, insert seed data.
        """

    @mark_fault_injected
    def inject_fault(self):
        """Swap the running cluster to the buggy image, then trigger the bug."""
        if self.crash_on_startup:
            logger.info("[GenericCustomBuild] Setting up preconditions before fault injection")
            self.setup_preconditions()
            logger.info(f"[GenericCustomBuild] Swapping to buggy image (expect startup crash): {self._custom_image}")
            self.app.inject_buggy_image_expect_crash(self._custom_image)
            logger.info("[GenericCustomBuild] Startup crash confirmed")
            return

        if self._predeployed_buggy:
            logger.info("[GenericCustomBuild] Buggy image already deployed at cluster start — skipping swap")
        else:
            logger.info(
                f"[GenericCustomBuild] Injecting fault: swapping {self.db_name} "
                f"cluster to {self._custom_image}"
            )
            self.app.inject_buggy_image(self._custom_image)
            logger.info("[GenericCustomBuild] Buggy image active")
        if self.reproducer:
            logger.info("[GenericCustomBuild] Running reproducer to trigger bug")
            try:
                self.app.run_reproducer(self.reproducer)
            except Exception as e:
                logger.warning(f"[GenericCustomBuild] run_reproducer raised (expected for crash bugs): {e}")
            if self.continuous_reproducer:
                self.app.deploy_continuous_reproducer(self.reproducer, self.expected_output)

    @mark_fault_injected
    def recover_fault(self):
        """Swap the cluster back to the stock image and wait for it to be Ready."""
        logger.info("[GenericCustomBuild] Recovering: restoring cluster to stock image")
        self.app.restore_stock_image(custom_image=self._custom_image)
        logger.info("[GenericCustomBuild] Recovery complete")

    # ── Internal ─────────────────────────────────────────────────────────────

    def _resolve_version_and_ref(self, spec: DBBuildSpec) -> tuple[str, str]:
        """Return (version, git_ref), resolving from issue URL or class attributes."""
        if self.issue_url:
            parsed = parse_issue(self.issue_url)
            if not self.root_cause_description and parsed.body:
                self.root_cause_description = (
                    f"{parsed.title}\n\n{parsed.body[:1000]}"
                ).strip()
            if not self.reproducer and parsed.reproducer:
                self.reproducer = parsed.reproducer
            if not self.expected_output and parsed.expected_output:
                self.expected_output = parsed.expected_output
            if parsed.crash_on_startup:
                self.crash_on_startup = True
            return parsed.version, parsed.git_ref

        if self.db_version and self.source_git_ref:
            return self.db_version, self.source_git_ref

        raise ValueError(
            f"{self.__class__.__name__} must set either 'issue_url' (auto mode) "
            f"or both 'db_version' and 'source_git_ref' (hand-crafted mode)."
        )
