"""Generic base for code-bug problems on any operator-managed distributed system.

The four-step workflow, run in ``__init__``:

1. Clone the target git revision of the upstream repo.
2. (Optional) Apply the bug patch by overlaying files from ``patch_dir``.
3. Compile — via the configured ``BuildRecipe`` — into a Docker image that's
   loaded onto cluster nodes.
4. Construct the operator-managed app via ``app_factory(image)``. The conductor
   calls ``app.deploy()`` later in the lifecycle; this class does **not** wait
   for Ready after deploy (readiness checks live in observer/oracles).

The agent can then inspect the patched source (bind-mounted at
``self.source_code_path``), diagnose the bug, edit the source, and rebuild.

Required subclass attributes:
    repo_url, source_git_ref, source_name, version,
    patch_dir (Path | None — None skips patching),
    build_recipe_factory (callable → BuildRecipe),
    app_factory (callable(image) → app),
    trigger (callable(app) → None),
    root_cause_file, root_cause_description.
"""

import logging
from collections.abc import Callable
from pathlib import Path

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.triggers import NoopTrigger
from sregym.service.build import BuildRecipe, ImageBuilder
from sregym.service.kubectl import KubeCtl
from sregym.service.source_manager import SourceManager
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class CodeBugProblem(Problem):
    """Code-bug problem whose target system is deployed by a k8s operator."""

    # --- subclass-required ---
    repo_url: str
    source_git_ref: str
    source_name: str
    version: str
    patch_dir: Path | None = None  # None → deploy the revision as-is
    build_recipe_factory: Callable[[], BuildRecipe]
    app_factory: Callable[[str], object]
    root_cause_file: str
    root_cause_description: str

    # --- subclass-optional ---
    trigger: Callable[[object], None] = NoopTrigger()
    allows_rebuild: bool = True  # agent may POST a rebuild request

    # OpenEBS is the de-facto storage class for the stateful operator-managed
    # workloads we target; subclasses can flip this off if they don't need it.
    _requires_openebs: bool = True

    def requires_openebs(self) -> bool:
        return self._requires_openebs

    def __init__(self):
        # Step 1: clone at the target revision.
        source_manager = SourceManager()
        source_path = source_manager.ensure_source(
            repo_url=self.repo_url,
            git_ref=self.source_git_ref,
            name=self.source_name,
        )
        # Reset so a prior agent's edits don't contaminate this run.
        source_manager.reset_source(source_path)

        # Steps 2 + 3: patch (optional) + compile + bake image.
        recipe = self.build_recipe_factory()
        patch_dir = Path(self.patch_dir) if self.patch_dir is not None else None
        image = ImageBuilder(recipe).build(source_path, patch_dir)
        logger.info(f"[CodeBug] Using image: {image}")

        # Step 4: construct app. Conductor will call ``deploy()`` later —
        # when it does, no readiness wait happens (by design).
        app = self.app_factory(image)
        super().__init__(app=app, namespace=app.namespace)
        self.kubectl = KubeCtl()
        self.source_code_path = source_path

        self.root_cause = self.build_structured_root_cause(
            component=f"source/{self.root_cause_file}",
            namespace=self.namespace,
            description=self.root_cause_description,
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        # Mitigation oracle is attached by subclasses that want it (OOM, crash, …).
        self.mitigation_oracle = None

    @mark_fault_injected
    def inject_fault(self):
        self.trigger(self.app)

    @mark_fault_injected
    def recover_fault(self):
        """Default no-op — the bug lives in source, not in injected runtime state."""
        logger.info("[CodeBug] No fault recovery needed (source-code bug)")
