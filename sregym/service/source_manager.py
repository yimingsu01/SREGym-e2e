"""Manages cloning and checking out source code on the host for bind-mounting into agent containers."""

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Default cache directory for cloned source repos
_DEFAULT_CACHE_DIR = Path("/tmp/sregym-sources")


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
            logger.info(f"Source already cached at {source_dir}")
            return source_dir

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

        # If that failed (e.g. ref is a commit SHA), do a full clone + checkout
        logger.info(f"Shallow clone failed for ref '{git_ref}', falling back to full clone...")
        # Clean up failed shallow clone
        subprocess.run(["rm", "-rf", str(source_dir)], check=True)
        source_dir.mkdir(parents=True, exist_ok=True)

        subprocess.run(
            ["git", "clone", repo_url, str(source_dir)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "checkout", git_ref],
            cwd=str(source_dir),
            check=True,
            capture_output=True,
            text=True,
        )
        logger.info(f"Full clone + checkout successful at {source_dir}")
        return source_dir
