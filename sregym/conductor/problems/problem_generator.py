"""Generate a runnable problem file from a GitHub issue URL.

Usage:
    from sregym.conductor.problems.problem_generator import ProblemGenerator

    problem_id = ProblemGenerator.generate("https://github.com/apache/cassandra/issues/20108")
    # → writes  sregym/conductor/problems/auto_cassandra_20108.py
    # → returns "auto_cassandra_20108"

The generated file is auto-discovered by ProblemRegistry on next instantiation,
so the problem can be run immediately:

    conductor.run_problem(problem_id)
"""

import logging
import re
import textwrap
from pathlib import Path

from sregym.service.issue_parser import ParsedIssue, parse_issue
from sregym.service.reproducer_extractor import repair_reproducer
from sregym.service.reproducer_validator import validate_reproducer, validation_required

_MAX_REPAIR_ATTEMPTS = 2

logger = logging.getLogger(__name__)

_PROBLEMS_DIR = Path(__file__).parent


class ProblemGenerator:

    @staticmethod
    def generate(issue_url: str) -> str:
        """Parse issue, validate reproducer, write problem file, return problem_id.

        Idempotent — if the file already exists it is overwritten so that
        re-running with the same URL refreshes the description.

        Validation: after extraction, the reproducer is run inside a stock
        (unfixed) container and its output compared to the description's
        buggy_output / correct_output. A validation failure aborts generation
        by default; set ``SREGYM_SKIP_REPRODUCER_VALIDATION=1`` to convert
        failures into warnings.
        """
        parsed = parse_issue(issue_url)
        ProblemGenerator._validate(parsed, issue_url)
        problem_id = ProblemGenerator._problem_id(parsed, issue_url)
        class_name = ProblemGenerator._class_name(parsed, issue_url)
        file_path = _PROBLEMS_DIR / f"{problem_id}.py"

        source = ProblemGenerator._render(
            issue_url=issue_url,
            parsed=parsed,
            class_name=class_name,
        )
        file_path.write_text(source)
        return problem_id

    # ── Validation ────────────────────────────────────────────────────────────

    @staticmethod
    def _validate(parsed: ParsedIssue, issue_url: str) -> None:
        """Run the reproducer against a stock buggy container; raise if it doesn't trigger the bug.

        On validation failure, asks the LLM to repair the reproducer and re-validates,
        up to ``_MAX_REPAIR_ATTEMPTS`` times. A successful repair mutates
        ``parsed.reproducer`` so the rendered problem file contains the fixed version.
        """
        if parsed.crash_on_startup or not parsed.reproducer:
            return

        result = validate_reproducer(
            spec=parsed.spec,
            version=parsed.version,
            reproducer=parsed.reproducer,
            buggy_output=parsed.buggy_output,
            correct_output=parsed.correct_output,
        )
        if result.skipped:
            logger.warning(
                f"[ProblemGenerator] reproducer validation skipped: {result.reason}"
            )
            return
        if result.bug_reproduced:
            logger.info(
                f"[ProblemGenerator] reproducer validated: {result.reason}"
            )
            return

        for attempt in range(1, _MAX_REPAIR_ATTEMPTS + 1):
            logger.warning(
                f"[ProblemGenerator] reproducer failed validation "
                f"(attempt {attempt}/{_MAX_REPAIR_ATTEMPTS}); asking LLM to repair. "
                f"reason: {result.reason}"
            )
            repaired = repair_reproducer(
                body=parsed.body,
                reproducer=parsed.reproducer,
                actual_output=result.actual_output,
                buggy_output=parsed.buggy_output,
                correct_output=parsed.correct_output,
            )
            if not repaired or repaired == parsed.reproducer:
                logger.warning(
                    "[ProblemGenerator] LLM returned no usable repair; stopping retries"
                )
                break

            parsed.reproducer = repaired
            result = validate_reproducer(
                spec=parsed.spec,
                version=parsed.version,
                reproducer=parsed.reproducer,
                buggy_output=parsed.buggy_output,
                correct_output=parsed.correct_output,
            )
            if result.skipped:
                logger.warning(
                    f"[ProblemGenerator] re-validation after repair skipped: {result.reason}"
                )
                return
            if result.bug_reproduced:
                logger.info(
                    f"[ProblemGenerator] repaired reproducer validated on attempt {attempt}: "
                    f"{result.reason}"
                )
                return

        msg = (
            f"Reproducer for {issue_url} did not trigger the bug against the stock "
            f"(buggy) image, even after {_MAX_REPAIR_ATTEMPTS} LLM repair attempt(s).\n"
            f"  reason: {result.reason}\n"
            f"  last reproducer: {parsed.reproducer!r}\n"
            f"  expected buggy_output: {parsed.buggy_output!r}\n"
            f"  expected correct_output: {parsed.correct_output!r}\n"
            f"  actual output: {result.actual_output[:500]!r}\n"
            f"Fix the reproducer in the generated file, or set "
            f"SREGYM_SKIP_REPRODUCER_VALIDATION=1 to bypass validation."
        )
        if validation_required():
            raise ValueError(msg)
        logger.warning(f"[ProblemGenerator] {msg}")

    # ── Rendering ─────────────────────────────────────────────────────────────

    @staticmethod
    def _render(issue_url: str, parsed: ParsedIssue, class_name: str) -> str:
        description = ProblemGenerator._safe_description(parsed)

        # 16 spaces = 12 (template common prefix) + 4 (class body indent)
        # so textwrap.dedent strips 12 and leaves 4 in the output file.
        extra_attrs = ""
        extra_methods = ""

        if parsed.crash_on_startup:
            extra_attrs += "\n                crash_on_startup = True"
            # Preconditions cannot be reliably auto-extracted; emit a stub.
            # 16 spaces before 'def' → dedent strips 12 → 4 spaces in output (class body)
            # 20 spaces before content → dedent strips 12 → 8 spaces in output (method body)
            extra_methods = (
                f"\n\n                def setup_preconditions(self):\n"
                f"                    pass  # TODO: self.app.run_reproducer(\"<command to enable the mode that causes the crash>\")"
            )
        elif parsed.reproducer:
            extra_attrs = (
                f"\n                reproducer = {repr(parsed.reproducer)}"
                f"\n                continuous_reproducer = True"
            )
            if parsed.expected_output:
                extra_attrs += f"\n                expected_output = {repr(parsed.expected_output)}"

        return textwrap.dedent(f'''\
            """Auto-generated from {issue_url}

            Title: {parsed.title}
            """
            from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


            class {class_name}(GenericCustomBuildProblem):
                db_name   = "{parsed.spec.name}"
                issue_url = "{issue_url}"
                root_cause_description = (
                    "{description}"
                ){extra_attrs}{extra_methods}
        ''')

    # ── Naming ────────────────────────────────────────────────────────────────

    @staticmethod
    def _issue_number(issue_url: str) -> str:
        # GitHub: /issues/2213
        m = re.search(r"/issues/(\d+)", issue_url)
        if m:
            return m.group(1)
        # Jira: /browse/ZOOKEEPER-2213
        m = re.search(r"/browse/[A-Z][A-Z0-9]+-(\d+)", issue_url, re.IGNORECASE)
        if m:
            return m.group(1)
        raise ValueError(f"Cannot extract issue number from: {issue_url}")

    @staticmethod
    def _problem_id(parsed: ParsedIssue, issue_url: str) -> str:
        number = ProblemGenerator._issue_number(issue_url)
        return f"auto_{parsed.spec.name}_{number}"

    @staticmethod
    def _class_name(parsed: ParsedIssue, issue_url: str) -> str:
        number = ProblemGenerator._issue_number(issue_url)
        db = parsed.spec.name.capitalize()
        return f"Auto{db}{number}"

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _safe_description(parsed: ParsedIssue) -> str:
        """Build a single-line description safe to embed in a Python string literal."""
        raw = f"{parsed.title}. {parsed.body[:800]}" if parsed.body else parsed.title
        # Collapse whitespace and escape quotes
        cleaned = re.sub(r"\s+", " ", raw).strip()
        return cleaned.replace("\\", "\\\\").replace('"', '\\"')
