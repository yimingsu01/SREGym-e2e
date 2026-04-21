"""Validate that an extracted reproducer actually triggers a bug.

Runs the reproducer inside a temp Docker container of the stock (unfixed)
database image and compares its output against what the bug description said
a buggy binary should produce. Lets us catch auto-extraction mistakes (wrong
code block picked, contrast case extracted instead of buggy case) before a
problem file is written.

Ground-truth check only — when the image or Docker is unavailable the result
is marked skipped and callers should treat it as inconclusive, not as failure.
"""

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass

from sregym.service.db_build_spec import DBBuildSpec

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    bug_reproduced: bool           # True if output matched buggy_output (or clearly differed from correct_output).
    skipped: bool = False          # True when validation could not run at all.
    reason: str = ""               # Short human-readable explanation.
    actual_output: str = ""        # Raw reproducer output (for logging on failure).


# ── Public API ────────────────────────────────────────────────────────────────

def validate_reproducer(
    spec: DBBuildSpec,
    version: str,
    reproducer: str | None,
    buggy_output: str | None,
    correct_output: str | None,
) -> ValidationResult:
    if not reproducer:
        return ValidationResult(False, skipped=True, reason="no reproducer to validate")

    if not _docker_available():
        return ValidationResult(False, skipped=True, reason="docker CLI not available")

    if spec.name == "mongodb":
        return _validate_mongodb(spec, version, reproducer, buggy_output, correct_output)

    return ValidationResult(
        False, skipped=True, reason=f"reproducer validation not implemented for db={spec.name!r}",
    )


# ── MongoDB validator ─────────────────────────────────────────────────────────

_READY_TIMEOUT_S = 40
_REPRO_TIMEOUT_S = 60


def _validate_mongodb(
    spec: DBBuildSpec,
    version: str,
    reproducer: str,
    buggy_output: str | None,
    correct_output: str | None,
) -> ValidationResult:
    image = spec.resolved_base_image(version)

    manifest = subprocess.run(
        ["docker", "manifest", "inspect", image],
        capture_output=True, text=True,
    )
    if manifest.returncode != 0:
        return ValidationResult(
            False, skipped=True,
            reason=f"stock image {image!r} not on registry — cannot validate",
        )

    container = f"sregym-validate-mongodb-{int(time.time() * 1000)}"
    logger.info(f"[ReproducerValidator] starting {image} as {container}")
    run = subprocess.run(
        ["docker", "run", "--rm", "-d", "--name", container, image],
        capture_output=True, text=True,
    )
    if run.returncode != 0:
        return ValidationResult(
            False, skipped=True,
            reason=f"docker run failed: {run.stderr.strip()[:200]}",
        )

    try:
        if not _mongodb_wait_ready(container):
            return ValidationResult(
                False, skipped=True,
                reason=f"mongod in {container!r} never became ready in {_READY_TIMEOUT_S}s",
            )

        exec_r = subprocess.run(
            ["docker", "exec", container, "mongosh", "--quiet", "--eval", reproducer],
            capture_output=True, text=True, timeout=_REPRO_TIMEOUT_S,
        )
        output = exec_r.stdout + ("\n" + exec_r.stderr if exec_r.stderr else "")
        logger.info(
            f"[ReproducerValidator] reproducer output (rc={exec_r.returncode}, "
            f"{len(output)}B): {output[:400]!r}"
        )
        return _compare_outputs(output, buggy_output, correct_output)

    except subprocess.TimeoutExpired:
        return ValidationResult(
            False, skipped=True,
            reason=f"reproducer exceeded {_REPRO_TIMEOUT_S}s timeout",
        )
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)


def _mongodb_wait_ready(container: str) -> bool:
    deadline = time.time() + _READY_TIMEOUT_S
    while time.time() < deadline:
        r = subprocess.run(
            ["docker", "exec", container, "mongosh", "--quiet", "--eval",
             "db.runCommand({ping:1}).ok"],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and "1" in r.stdout:
            return True
        time.sleep(1)
    return False


# ── Output comparison ─────────────────────────────────────────────────────────

def _compare_outputs(
    actual: str,
    buggy_output: str | None,
    correct_output: str | None,
) -> ValidationResult:
    actual_norm = _normalize(actual)
    buggy_norm = _normalize(buggy_output) if buggy_output else None
    correct_norm = _normalize(correct_output) if correct_output else None

    matches_buggy = bool(buggy_norm) and buggy_norm in actual_norm
    matches_correct = bool(correct_norm) and correct_norm in actual_norm

    if matches_buggy and not matches_correct:
        return ValidationResult(
            True, reason="output matches buggy_output → bug is present", actual_output=actual,
        )
    if matches_correct and not matches_buggy:
        return ValidationResult(
            False,
            reason=(
                "output matches correct_output — reproducer runs the non-buggy code path "
                "(likely the wrong code block was extracted)"
            ),
            actual_output=actual,
        )
    if matches_buggy and matches_correct:
        return ValidationResult(
            True,
            reason="output contains both buggy and correct substrings — assuming bug present",
            actual_output=actual,
        )
    if not buggy_norm and not correct_norm:
        return ValidationResult(
            False, skipped=True,
            reason="LLM extracted neither buggy_output nor correct_output — cannot decide",
            actual_output=actual,
        )
    if buggy_output and _looks_like_crash(buggy_output) and _looks_like_crash(actual):
        return ValidationResult(
            True,
            reason="both buggy_output and actual indicate mongod process death — bug reproduced",
            actual_output=actual,
        )
    return ValidationResult(
        False,
        reason=(
            "output matches neither extracted buggy_output nor correct_output — "
            "reproducer is likely wrong OR extracted outputs don't match mongosh's actual formatting"
        ),
        actual_output=actual,
    )


def _normalize(s: str) -> str:
    """Fuzzy normalization for substring matching.

    Mongosh always pretty-prints with spaces around braces/brackets, but Jira
    descriptions often inline (``{a:[]}`` vs ``{ a: [] }``). Also unifies smart
    quotes and collapses ``"`` to ``'`` since mongosh emits single quotes.
    """
    s = s.replace("\u2018", "'").replace("\u2019", "'").replace("\u201C", '"').replace("\u201D", '"')
    s = s.replace('"', "'")
    s = re.sub(r"\s+", "", s).strip()
    return s


# Markers of mongod process death. Legacy `mongo` shell and modern `mongosh`
# print different text for the same crash (e.g. "Connection closed by peer"
# vs "MongoNetworkError: connection N to HOST closed"); either side is enough.
_CRASH_MARKERS = (
    "connection closed by peer",
    "mongonetworkerror",
    "econnreset",
    "socket exception",
    "connection was closed",
    "network error while attempting",
    "connection ended",
)


def _looks_like_crash(s: str) -> bool:
    low = s.lower()
    return any(m in low for m in _CRASH_MARKERS)


# ── Environment ───────────────────────────────────────────────────────────────

def _docker_available() -> bool:
    try:
        r = subprocess.run(["docker", "version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def validation_required() -> bool:
    """Whether a failed validation should abort problem generation.

    Default: True (strict). Set SREGYM_SKIP_REPRODUCER_VALIDATION=1 to opt out
    of strict mode — validation still runs, but failures become warnings.
    """
    return os.environ.get("SREGYM_SKIP_REPRODUCER_VALIDATION", "") != "1"
