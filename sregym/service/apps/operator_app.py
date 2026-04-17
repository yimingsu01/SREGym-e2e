"""Abstract base for Kubernetes-operator-managed distributed systems.

Future systems (Kafka/Strimzi, Postgres/CNPG, Elasticsearch/ECK, …) subclass
this. The existing ``Cassandra`` app predates this base and already satisfies
its protocol duck-typed — it is intentionally left alone so the four other
Cassandra problems keep working.

Subclasses implement three hooks:

- ``operator_prereqs()``  → list of callables run before operator install
  (e.g. install cert-manager for K8ssandra).
- ``install_operator()``  → helm-install the operator.
- ``build_cluster_manifest(custom_image)`` → cluster CR YAML.

``deploy()`` runs prereqs → install operator → apply CR. It deliberately does
**not** wait for readiness — that belongs to observer/oracles.
"""

import logging
import subprocess
from abc import abstractmethod
from collections.abc import Callable

from sregym.service.apps.base import Application
from sregym.service.kubectl import KubeCtl

logger = logging.getLogger("all.application")


class OperatorManagedApp(Application):
    """Base for distributed systems whose lifecycle is driven by a k8s operator."""

    def __init__(self, config_file: str):
        super().__init__(config_file)
        self.kubectl = KubeCtl()
        self.custom_image: str | None = None

    # ------------------------------------------------------------------
    # Hooks for subclasses
    # ------------------------------------------------------------------

    def operator_prereqs(self) -> list[Callable[[], None]]:
        """Callables run before operator install (e.g. install cert-manager)."""
        return []

    @abstractmethod
    def install_operator(self) -> None:
        """Helm-install (or upgrade --install) the operator."""

    @abstractmethod
    def build_cluster_manifest(self, custom_image: str | None) -> str:
        """Return the cluster CR YAML, optionally pointing at ``custom_image``."""

    @abstractmethod
    def cluster_cr_kind(self) -> str:
        """CRD kind for ``kubectl delete`` during teardown (e.g. ``"kafka"``)."""

    @abstractmethod
    def cluster_cr_name(self) -> str:
        """Name of the cluster CR instance (e.g. ``self.cluster_name``)."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def deploy(self) -> None:
        """Run prereqs → install operator → apply cluster CR. Does not wait for Ready."""
        logger.info(f"[OperatorApp] Deploying {self.name or self.__class__.__name__}")
        for prereq in self.operator_prereqs():
            prereq()
        self.install_operator()
        self._apply_cluster_cr()
        logger.info("[OperatorApp] Cluster CR applied — not waiting for ready")

    def _apply_cluster_cr(self) -> None:
        subprocess.run(
            f"kubectl create namespace {self.namespace} --dry-run=client -o yaml | kubectl apply -f -",
            shell=True,
            check=False,
        )
        manifest = self.build_cluster_manifest(self.custom_image)
        result = subprocess.run(
            "kubectl apply -f -",
            shell=True,
            input=manifest,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Cluster CR apply failed:\nstdout: {result.stdout}\nstderr: {result.stderr}")

    def cleanup(self) -> None:
        """Delete cluster CR + namespace. Operator-specific teardown in subclass."""
        subprocess.run(
            f"kubectl delete {self.cluster_cr_kind()} {self.cluster_cr_name()} -n {self.namespace} --ignore-not-found",
            shell=True,
            check=False,
        )
        self.kubectl.delete_namespace(self.namespace)

    def start_workload(self) -> None:
        pass

    def create_workload(self, **kwargs) -> None:
        pass
