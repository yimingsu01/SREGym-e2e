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

import re
import textwrap
from pathlib import Path

from sregym.service.issue_parser import ParsedIssue, parse_issue

_PROBLEMS_DIR = Path(__file__).parent


class ProblemGenerator:

    @staticmethod
    def generate(issue_url: str) -> str:
        """Parse issue, write problem file, return problem_id.

        Idempotent — if the file already exists it is overwritten so that
        re-running with the same URL refreshes the description.
        """
        parsed = parse_issue(issue_url)
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

    # ── Rendering ─────────────────────────────────────────────────────────────

    @staticmethod
    def _render(issue_url: str, parsed: ParsedIssue, class_name: str) -> str:
        description = ProblemGenerator._safe_description(parsed)
        reproducer_attrs = ""
        if parsed.reproducer:
            # 16 spaces = 12 (template common prefix) + 4 (class body indent)
            # so textwrap.dedent strips 12 and leaves 4 in the output file.
            reproducer_attrs = (
                f"\n                reproducer = {repr(parsed.reproducer)}"
                f"\n                continuous_reproducer = True"
            )
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
                ){reproducer_attrs}
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
