"""Manages cloning and checking out source code on the host for bind-mounting into agent containers."""

import contextlib
import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Default cache directory for cloned source repos
_DEFAULT_CACHE_DIR = Path("/tmp/sregym-sources")

# Default directory for git-free agent source copies (see export_clean_copy).
_DEFAULT_AGENT_SRC_DIR = Path("/tmp/sregym-agent-src")

# Version-control / changelog / issue-tracker metadata stripped from the agent copy
# so an agent cannot recover the upstream fix or pin the exact version. Build files
# (build.xml, pom.xml, go.mod, Makefile, ...) are intentionally KEPT — they are needed
# to compile, and a version property alone is not the fix.
_DEFAULT_SCRUB = (
    ".git",
    ".github",
    ".gitlab-ci.yml",
    ".circleci",
    "CHANGES",
    "CHANGES.txt",
    "CHANGES.md",
    "CHANGELOG",
    "CHANGELOG.md",
    "CHANGELOG.txt",
    "NEWS",
    "NEWS.txt",
)


class SourceManager:
    """Clone and checkout source code at a specific git ref.

    Clones are cached on the host so repeated runs don't re-download.
    Each (repo, ref) pair gets its own directory.
    """

    def __init__(self, cache_dir: Path | None = None):
        self.cache_dir = cache_dir or _DEFAULT_CACHE_DIR

    def ensure_source(self, repo_url: str, git_ref: str, name: str | None = None) -> Path:
        """Ensure source code is cloned and checked out at the given ref.

        Args:
            repo_url: Git repository URL (e.g. https://github.com/apache/cassandra.git)
            git_ref: Git ref to checkout — tag, branch, or commit SHA
                     (e.g. "cassandra-5.0.2", "trunk", "4fc8bb29fc...")
            name: Optional short name for the directory (defaults to repo basename)

        Returns:
            Path to the checked-out source tree on the host.
        """
        if name is None:
            name = repo_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")

        # Directory name encodes both repo and ref for isolation
        safe_ref = git_ref.replace("/", "_")
        source_dir = self.cache_dir / f"{name}-{safe_ref}"

        if source_dir.exists() and (source_dir / ".git").exists():
            r = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(source_dir),
                capture_output=True,
                text=True,
            )
            if r.returncode == 0:
                logger.info(f"Source already cached at {source_dir}")
                return source_dir
            logger.warning(f"Incomplete checkout at {source_dir} — re-cloning")

        source_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Cloning {repo_url} at ref '{git_ref}' into {source_dir}...")

        # Try shallow clone with the ref as a branch/tag first
        result = subprocess.run(
            ["git", "clone", "--branch", git_ref, "--depth=1", repo_url, str(source_dir)],
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            logger.info(f"Shallow clone successful at {source_dir}")
            return source_dir

        # Shallow clone failed (e.g. ref is a commit SHA, not a branch/tag).
        # Fall back to fetch + checkout in-place — avoids deleting root-owned
        # build artifacts that docker run may have created inside the directory.
        logger.info(f"Shallow clone failed for ref '{git_ref}', falling back to fetch + checkout...")

        if not (source_dir / ".git").exists():
            subprocess.run(
                ["git", "init", str(source_dir)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "remote", "add", "origin", repo_url],
                cwd=str(source_dir),
                check=True,
                capture_output=True,
                text=True,
            )

        # GitHub rejects `git fetch --depth=1 origin <SHA>` for arbitrary commits.
        # Try a partialclone filter first (works on GitHub), then fall back to a
        # full unshallow fetch which is slow but always succeeds.
        fetched = False
        for fetch_args in (
            ["--depth=1", "--filter=blob:none", git_ref],
            ["--depth=1", git_ref],
            [git_ref],
        ):
            r = subprocess.run(
                ["git", "fetch", "origin"] + fetch_args,
                cwd=str(source_dir),
                capture_output=True,
                text=True,
            )
            if r.returncode == 0:
                fetched = True
                break

        if not fetched:
            raise RuntimeError(f"Could not fetch ref '{git_ref}' from {repo_url}. Last error: {r.stderr.strip()}")

        # Clear any leftover files from a prior partial run so checkout
        # doesn't abort with "untracked working tree files would be overwritten".
        subprocess.run(
            ["git", "clean", "-fdx"],
            cwd=str(source_dir),
            capture_output=True,
            text=True,
        )
        r = subprocess.run(
            ["git", "checkout", "FETCH_HEAD"],
            cwd=str(source_dir),
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"git checkout FETCH_HEAD failed in {source_dir}: {r.stderr.strip()}")
        logger.info(f"Fetch + checkout successful at {source_dir}")
        return source_dir

    def export_clean_copy(
        self,
        source_dir: Path,
        dest_dir: Path | None = None,
        scrub: tuple[str, ...] = _DEFAULT_SCRUB,
    ) -> Path:
        """Materialize a git-free, identity-scrubbed working copy for the agent mount.

        The agent must be able to edit and rebuild this tree, but must NOT be able to
        recover the upstream fix from it. The returned copy therefore:

        - contains the full checked-out working tree (so it still compiles),
        - has **no** ``.git`` directory (no history, tags, or ``origin`` remote — the
          agent cannot ``git log`` / ``git diff`` / ``git fetch`` / ``git checkout`` the
          fix, and patch-injected bugs are no longer visible as an uncommitted diff), and
        - drops the version/issue-revealing metadata in ``scrub`` (changelogs, CI config)
          while KEEPING build files needed to compile.

        Re-running overwrites ``dest_dir`` from ``source_dir``, so this doubles as the
        per-run reset: any edits a previous agent made are discarded.

        Args:
            source_dir: the host git clone (kept intact; never mounted to the agent).
            dest_dir: where to write the clean copy. Defaults to
                ``/tmp/sregym-agent-src/<source_dir.name>``.
            scrub: top-level/nested names (files or dirs) to drop from the copy.

        Returns:
            Path to the git-free, scrubbed working copy to bind-mount at ``/opt/source``.
        """
        source_dir = Path(source_dir)
        if not source_dir.exists():
            raise FileNotFoundError(f"Source directory does not exist: {source_dir}")

        if dest_dir is None:
            dest_dir = _DEFAULT_AGENT_SRC_DIR / source_dir.name
        dest_dir = Path(dest_dir)

        # Fresh each run: a stale dest must never leak a prior agent's edits.
        if dest_dir.exists():
            shutil.rmtree(dest_dir, ignore_errors=True)
        dest_dir.parent.mkdir(parents=True, exist_ok=True)

        # ignore_patterns matches entry names at every directory level, so nested
        # ``.git`` dirs (e.g. submodules) and scrubbed files are excluded everywhere.
        shutil.copytree(
            source_dir,
            dest_dir,
            ignore=shutil.ignore_patterns(*scrub),
            symlinks=True,
        )

        # Defensive belt-and-suspenders: ensure no .git survived (e.g. odd edge case).
        leftover_git = dest_dir / ".git"
        if leftover_git.exists():
            shutil.rmtree(leftover_git, ignore_errors=True)

        logger.info(f"Exported clean (git-free) agent source copy to {dest_dir}")

        # Snapshot the pristine export as the anti-cheat audit baseline before the
        # agent can edit it (best-effort; never blocks the export).
        with contextlib.suppress(Exception):
            from sregym.service.source_audit import capture_baseline

            capture_baseline(dest_dir)

        return dest_dir

    def reset_source(self, source_dir: Path) -> None:
        """Reset the source directory to its original git state.

        Discards all local modifications (uncommitted changes) so patches
        can be cleanly re-applied. This ensures each benchmark run starts
        from the same baseline source code.
        """
        source_dir = Path(source_dir)
        if not (source_dir / ".git").exists():
            logger.warning(f"Cannot reset {source_dir} — not a git repository")
            return

        logger.info(f"Resetting source at {source_dir} to original git state...")

        # Discard all uncommitted changes (modified and untracked files in tracked paths)
        result = subprocess.run(
            ["git", "checkout", "--", "."],
            cwd=str(source_dir),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning(f"git checkout failed: {result.stderr}")

        # Clean untracked AND gitignored files (e.g. build/ directory).
        # -f: force, -d: directories, -x: also remove gitignored files.
        # Without -x, a previous agent's compiled build/ artifacts persist
        # and `ant jar` (incremental) may skip recompilation.
        result = subprocess.run(
            ["git", "clean", "-fdx"],
            cwd=str(source_dir),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning(f"git clean failed: {result.stderr}")

        logger.info(f"Source reset complete at {source_dir}")
