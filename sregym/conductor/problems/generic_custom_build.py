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
    # When True, re-run the reproducer in a background loop every
    # `reproducer_interval` seconds so the bug stays continuously observable.
    continuous_reproducer: bool = False

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

        build_mgr = GenericDBBuildManager(spec, source_path, version)
        if self.patch_dir is not None:
            self._custom_image = build_mgr.build_with_patches(Path(self.patch_dir))
        else:
            self._custom_image = build_mgr.build_from_directory()

        logger.info(f"[GenericCustomBuild] Using image: {self._custom_image}")

        app = GenericDBApplication(spec, version)
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

    @mark_fault_injected
    def inject_fault(self):
        """Swap the running cluster to the buggy image, then trigger the bug."""
        logger.info(
            f"[GenericCustomBuild] Injecting fault: swapping {self.db_name} "
            f"cluster to {self._custom_image}"
        )
        self.app.inject_buggy_image(self._custom_image)
        logger.info("[GenericCustomBuild] Buggy image active")
        if self.reproducer:
            logger.info("[GenericCustomBuild] Running reproducer to trigger bug")
            self.app.run_reproducer(self.reproducer)
            if self.continuous_reproducer:
                self.app.deploy_continuous_reproducer(self.reproducer)

    @mark_fault_injected
    def recover_fault(self):
        """No-op — conductor tears down the namespace between problems."""
        logger.info("[GenericCustomBuild] No fault recovery needed")

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
            return parsed.version, parsed.git_ref

        if self.db_version and self.source_git_ref:
            return self.db_version, self.source_git_ref

        raise ValueError(
            f"{self.__class__.__name__} must set either 'issue_url' (auto mode) "
            f"or both 'db_version' and 'source_git_ref' (hand-crafted mode)."
        )
