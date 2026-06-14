"""Audit trail for agent edits to bind-mounted database source (anti-cheat).

A pristine baseline manifest (relative path → sha256) of the clean-room source export
is captured BEFORE the agent can edit it. On every rebuild the live source is diffed
against that baseline and a tamper-evident JSON record is appended. This makes
reward-hacking attempts visible after the fact — for example a rebuild that changed no
source at all (hoping to pass on flaky readiness), or one that touched only files
outside the source tree.

The baseline and the audit log live on the host OUTSIDE the agent's ``/opt/source``
mount, so the agent (even as root in its container) can neither read the baseline nor
tamper with the record.
"""

import hashlib
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Host-side audit location (never under the agent's /opt/source mount).
_AUDIT_DIR = Path.home() / ".sregym" / "audit"
_REBUILD_LOG = _AUDIT_DIR / "rebuilds.jsonl"

# Path components that indicate a plausible source-level change. Used only to annotate
# records (never to block), since the exact layout varies by database/build system.
_SOURCE_HINT_DIRS = ("src", "lib", "pkg", "server", "java", "go", "cpp", "rust")


def _iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            yield p


def snapshot_manifest(root: Path, max_bytes: int = 50 * 1024 * 1024) -> dict[str, str]:
    """Return ``{relative_path: sha256}`` for every regular file under ``root``.

    Files larger than ``max_bytes`` are recorded by size rather than hashed, so a huge
    build artifact never makes the snapshot pathologically slow.
    """
    root = Path(root)
    manifest: dict[str, str] = {}
    for p in _iter_files(root):
        try:
            rel = str(p.relative_to(root))
            size = p.stat().st_size
            if size > max_bytes:
                manifest[rel] = f"size:{size}"
                continue
            h = hashlib.sha256()
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            manifest[rel] = h.hexdigest()
        except OSError as e:
            logger.debug("audit: skipping %s: %s", p, e)
    return manifest


def diff_manifests(baseline: dict[str, str], current: dict[str, str]) -> dict[str, list[str]]:
    """Return added/removed/modified relative paths between two manifests."""
    b, c = set(baseline), set(current)
    return {
        "added": sorted(c - b),
        "removed": sorted(b - c),
        "modified": sorted(k for k in (b & c) if baseline[k] != current[k]),
    }


def baseline_path_for(export_dir: Path) -> Path:
    """Deterministic host-side baseline-manifest path for a given export directory."""
    key = hashlib.sha256(str(Path(export_dir).resolve()).encode()).hexdigest()[:16]
    return _AUDIT_DIR / f"baseline-{key}.json"


def capture_baseline(export_dir: Path) -> Path | None:
    """Snapshot the pristine export and persist it as the per-run baseline. Best-effort."""
    try:
        _AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        manifest = snapshot_manifest(export_dir)
        path = baseline_path_for(export_dir)
        path.write_text(json.dumps(manifest))
        logger.info("audit: captured baseline (%d files) for %s", len(manifest), export_dir)
        return path
    except Exception as e:
        logger.warning("audit: failed to capture baseline for %s: %s", export_dir, e)
        return None


def analyze_changes(changes: dict[str, list[str]]) -> list[str]:
    """Flag suspicious change-sets (advisory only)."""
    flags: list[str] = []
    if not (changes["added"] or changes["removed"] or changes["modified"]):
        flags.append("no-source-change")
    touched = changes["added"] + changes["modified"]
    if touched and not any(part in _SOURCE_HINT_DIRS for f in touched for part in Path(f).parts):
        flags.append("no-recognized-source-dir-touched")
    return flags


def record_rebuild(
    problem_id: str,
    export_dir: Path,
    image_tag: str | None = None,
    log_path: Path | None = None,
) -> dict | None:
    """Diff the live source against the baseline and append an audit record. Best-effort.

    Call this at the START of a rebuild (before compilation) so the diff reflects the
    agent's source edits rather than build artifacts. Returns the record, or None on error.
    """
    try:
        export_dir = Path(export_dir)
        base_path = baseline_path_for(export_dir)
        baseline = json.loads(base_path.read_text()) if base_path.exists() else {}
        current = snapshot_manifest(export_dir)
        changes = diff_manifests(baseline, current)
        record = {
            "ts": time.time(),
            "problem_id": problem_id,
            "image_tag": image_tag,
            "source_dir": str(export_dir),
            "baseline_files": len(baseline),
            "current_files": len(current),
            **changes,
            "flags": analyze_changes(changes) if baseline else ["no-baseline"],
        }
        path = Path(log_path) if log_path else _REBUILD_LOG
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")
        if record["flags"]:
            logger.warning("audit: rebuild for %s flagged %s", problem_id, record["flags"])
        else:
            logger.info(
                "audit: rebuild for %s changed %d file(s)",
                problem_id,
                len(changes["added"]) + len(changes["modified"]) + len(changes["removed"]),
            )
        return record
    except Exception as e:
        logger.warning("audit: failed to record rebuild for %s: %s", problem_id, e)
        return None
