"""Generic Kubernetes database application driven by a DBBuildSpec.

Lifecycle:
  deploy()             — install operator prereqs + Helm operator + stock cluster CR
  inject_buggy_image() — patch the running CR to use a custom-built image;
                         triggers a rolling restart so pods run the buggy binary
  cleanup()            — delete cluster CR, namespaces, PVs, operator

This class handles any database whose operator follows the standard pattern:
  - Installed via Helm
  - Managed via a single custom resource (CR)
  - Image swappable via a JSON merge-patch on the CR
"""

import json
import logging
import subprocess
import time

from sregym.service.db_build_spec import DBBuildSpec
from sregym.service.helm import Helm

logger = logging.getLogger("all.application")


def _run(cmd: str, input: str | None = None, check: bool = True) -> str:
    result = subprocess.run(
        cmd, shell=True, check=False,
        capture_output=True, text=True, input=input,
    )
    if result.stdout:
        logger.debug(result.stdout.strip())
    if result.stderr:
        logger.debug(result.stderr.strip())
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed (exit {result.returncode}): {cmd}\n"
            f"stdout: {result.stdout.strip()}\nstderr: {result.stderr.strip()}"
        )
    return result.stdout


class GenericDBApplication:
    """Deploy and manage a database cluster for any DB type registered in DB_REGISTRY."""

    def __init__(
        self,
        spec: DBBuildSpec,
        version: str,
        cluster_name: str | None = None,
    ):
        self.spec = spec
        self.version = version
        self.cluster_name = cluster_name or spec.default_cluster_name
        # CR and operator share the operator namespace (mirrors Cassandra behaviour).
        self.namespace = spec.operator_namespace
        self.name = spec.name

    # ── Public API ────────────────────────────────────────────────────────────

    def deploy(self):
        """Install operator + deploy stock cluster, wait for it to be ready."""
        logger.info(f"Deploying {self.spec.name} {self.version} via {self.spec.operator_chart}")
        if self.spec.prereqs_fn:
            self.spec.prereqs_fn()
        self._install_operator()
        self._deploy_cluster(custom_image=None)
        self._wait_for_cluster_ready()
        logger.info(f"{self.spec.name} cluster deployed")

    def inject_buggy_image(self, image_tag: str):
        """Patch the running cluster to use a custom-built (buggy) image.

        First gives the operator a chance to propagate the CR change itself.
        If the operator stalls (e.g. sets partition=1 or ignores spec.*.image),
        falls back to scaling it down and patching StatefulSets directly.
        """
        patch = self.spec.image_patch_fn(self.cluster_name, self.namespace, image_tag)
        patch_json = json.dumps(patch)
        logger.info(f"Swapping {self.spec.name} cluster to image: {image_tag}")
        _run(
            f"kubectl patch {self.spec.cr_kind} {self.cluster_name} "
            f"-n {self.namespace} --type=merge -p '{patch_json}'"
        )
        logger.info("Image patched — waiting for rolling restart")
        self._wait_for_image_rollout(image_tag)
        logger.info(f"Cluster ready with image: {image_tag}")

    def cleanup(self):
        """Delete cluster CR, namespaces, leftover PVs, and the operator."""
        logger.info(f"Cleaning up {self.spec.name} deployment")

        _run(
            f"kubectl delete {self.spec.cr_kind} {self.cluster_name} "
            f"-n {self.namespace} --ignore-not-found",
            check=False,
        )

        self._delete_namespace(self.namespace)

        # Remove any PVs still bound to this namespace.
        pvs = _run(
            f"kubectl get pv --no-headers | grep '{self.namespace}' || true",
            check=False,
        )
        for line in pvs.strip().splitlines():
            if line:
                pv_name = line.split()[0]
                subprocess.run(
                    f'kubectl patch pv {pv_name} -p \'{{"metadata":{{"finalizers":null}}}}\'',
                    shell=True, check=False,
                )
                subprocess.run(
                    f"kubectl delete pv {pv_name} --ignore-not-found",
                    shell=True, check=False,
                )

        subprocess.run(
            f"helm uninstall {self.spec.name}-operator -n {self.spec.operator_namespace} 2>/dev/null || true",
            shell=True, check=False,
        )
        self._delete_namespace(self.spec.operator_namespace)
        logger.info(f"{self.spec.name} cleanup complete")

    def run_reproducer(self, reproducer: str):
        """Run a reproducer script/query against the live cluster to trigger the bug."""
        if self.spec.run_reproducer_fn:
            self.spec.run_reproducer_fn(self.cluster_name, self.namespace, reproducer)
        else:
            logger.warning(f"No run_reproducer_fn defined for {self.spec.name} — skipping")

    def deploy_continuous_reproducer(self, reproducer: str):
        """Deploy a Deployment on the cluster that runs the reproducer in a loop."""
        if not self.spec.reproducer_workload_fn:
            logger.warning(f"No reproducer_workload_fn for {self.spec.name} — skipping continuous workload")
            return
        manifest = self.spec.reproducer_workload_fn(self.cluster_name, self.namespace, reproducer)
        _run("kubectl apply -f -", input=manifest)
        logger.info(
            f"Continuous reproducer workload '{self.cluster_name}-reproducer' "
            f"deployed in '{self.namespace}'"
        )

    def start_workload(self):
        pass

    def create_workload(self, **kwargs):
        pass

    # ── Internal ──────────────────────────────────────────────────────────────

    def _install_operator(self):
        logger.info(f"Installing {self.spec.operator_chart} in '{self.spec.operator_namespace}'")
        try:
            Helm.add_repo(self.spec.operator_helm_repo, self.spec.operator_helm_repo_url)
            Helm.repo_update()
        except RuntimeError as e:
            logger.warning(f"Helm repo setup issue (continuing with cached charts): {e}")

        existing = subprocess.run(
            f"helm status {self.spec.name}-operator -n {self.spec.operator_namespace}",
            shell=True, capture_output=True, text=True,
        )
        if existing.returncode == 0:
            if "deployed" in existing.stdout:
                logger.info(f"{self.spec.name} operator already deployed, skipping")
                return
            if "failed" in existing.stdout:
                subprocess.run(
                    f"helm uninstall {self.spec.name}-operator -n {self.spec.operator_namespace}",
                    shell=True, check=False,
                )

        helm_cmd = (
            f"helm upgrade --install {self.spec.name}-operator {self.spec.operator_chart} "
            f"--namespace {self.spec.operator_namespace} "
            f"--create-namespace "
            f"--set global.clusterScoped=true "
            f"{self.spec.operator_extra_helm_args} "
            f"--wait --timeout 5m"
        )
        for attempt in range(1, 4):
            try:
                _run(helm_cmd)
                break
            except RuntimeError as e:
                if attempt == 3 or "webhook" not in str(e):
                    raise
                logger.warning(f"Operator install attempt {attempt}/3 failed — retrying in 20s")
                time.sleep(20)

        logger.info(f"{self.spec.name} operator ready")

    def _deploy_cluster(self, custom_image: str | None):
        _run(
            f"kubectl create namespace {self.namespace} "
            f"--dry-run=client -o yaml | kubectl apply -f -"
        )
        manifest = self.spec.cluster_manifest_fn(
            self.cluster_name, self.namespace, self.version, custom_image
        )
        _run("kubectl apply -f -", input=manifest)
        logger.info(f"{self.spec.name} cluster CR applied to '{self.namespace}'")

    def _wait_for_cluster_ready(self, timeout: int = 600):
        """Wait until the cluster's own pods are Ready.

        Uses app.kubernetes.io/instance={cluster_name}, which all major operators
        apply to every pod they manage (TiDB, K8ssandra, etc.).

        Requires the pod count to be stable across two consecutive 15-second
        polls before declaring success.  This prevents returning early when the
        operator is still creating pods (e.g. PD ready before TiKV/TiDB exist).
        """
        label = f"app.kubernetes.io/instance={self.cluster_name}"
        logger.info(
            f"Waiting for cluster '{self.cluster_name}' pods "
            f"(label: {label}) to be Ready (up to {timeout}s)…"
        )
        deadline = time.time() + timeout
        last_count = 0
        stable_and_ready = 0

        while time.time() < deadline:
            out = subprocess.run(
                f"kubectl get pods -n {self.namespace} -l '{label}' --no-headers 2>/dev/null",
                shell=True, capture_output=True, text=True,
            )
            pods = [l for l in out.stdout.strip().splitlines() if l]
            count = len(pods)

            if count > 0 and count == last_count:
                r = subprocess.run(
                    f"kubectl wait pods -n {self.namespace} -l '{label}' "
                    f"--for=condition=Ready --timeout=5s",
                    shell=True, capture_output=True, text=True,
                )
                if r.returncode == 0:
                    stable_and_ready += 1
                    if stable_and_ready >= 2:
                        logger.info(f"Cluster '{self.cluster_name}' is Ready ({count} pods)")
                        return
                else:
                    stable_and_ready = 0
            else:
                stable_and_ready = 0

            last_count = count
            time.sleep(15)

        raise RuntimeError(
            f"Timeout ({timeout}s) waiting for cluster '{self.cluster_name}' "
            f"pods to be Ready in '{self.namespace}'"
        )

    def _wait_for_image_rollout(self, image_tag: str, timeout: int = 600):
        """Wait until at least one pod in the namespace runs image_tag and is Ready.

        Gives the operator up to 30 s to propagate the CR change on its own.
        If no pod shows the new image by then, falls back to a generic override:
          1. Scale down every Deployment in the operator namespace so the operator
             stops reconciling (and reverting) the StatefulSets.
          2. For every StatefulSet in the cluster namespace:
             - Reset partition to 0 so Kubernetes' own rolling-update fires.
             - Replace any container whose image matches the stock base image with
               image_tag (identified by resolved_base_image from the spec).
        This fallback is operator-agnostic: it uses no hardcoded names.
        """
        logger.info(
            f"Waiting for rolling update to image '{image_tag}' "
            f"in '{self.namespace}' (up to {timeout}s)…"
        )
        override_attempted = False
        deadline = time.time() + timeout
        operator_wait = time.time() + 30  # grace period before override

        while time.time() < deadline:
            if self._any_pod_has_image(image_tag):
                pod_name = self._find_pod_with_image(image_tag)
                if pod_name:
                    ready_r = subprocess.run(
                        f"kubectl wait pod/{pod_name} -n {self.namespace} "
                        f"--for=condition=Ready --timeout=5s",
                        shell=True, capture_output=True, text=True,
                    )
                    if ready_r.returncode == 0:
                        logger.info(f"Pod '{pod_name}' is Ready with image '{image_tag}'")
                        return

            if not override_attempted and time.time() > operator_wait:
                logger.info(
                    "Operator did not propagate image change — "
                    "falling back to direct StatefulSet override"
                )
                self._operator_override(image_tag)
                override_attempted = True

            time.sleep(15)

        raise RuntimeError(
            f"Timeout ({timeout}s) waiting for image '{image_tag}' "
            f"to roll out in '{self.namespace}'"
        )

    def _any_pod_has_image(self, image_tag: str) -> bool:
        out = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-o jsonpath='{{range .items[*]}}{{range .spec.containers[*]}}{{.image}} {{end}}{{end}}' "
            f"2>/dev/null",
            shell=True, capture_output=True, text=True,
        )
        return image_tag in out.stdout

    def _find_pod_with_image(self, image_tag: str) -> str | None:
        names_out = subprocess.run(
            f"kubectl get pods -n {self.namespace} --no-headers "
            f"-o jsonpath='{{range .items[*]}}{{.metadata.name}} {{end}}' 2>/dev/null",
            shell=True, capture_output=True, text=True,
        )
        for pod_name in names_out.stdout.strip().split():
            img_out = subprocess.run(
                f"kubectl get pod {pod_name} -n {self.namespace} "
                f"-o jsonpath='{{range .spec.containers[*]}}{{.image}} {{end}}' 2>/dev/null",
                shell=True, capture_output=True, text=True,
            )
            if image_tag in img_out.stdout:
                return pod_name
        return None

    def _operator_override(self, image_tag: str):
        """Scale down all operator Deployments, then directly patch StatefulSets."""
        # Scale down every Deployment in the operator namespace.
        deps_out = subprocess.run(
            f"kubectl get deployments -n {self.spec.operator_namespace} "
            f"--no-headers -o jsonpath='{{range .items[*]}}{{.metadata.name}} {{end}}' 2>/dev/null",
            shell=True, capture_output=True, text=True,
        )
        for dep in deps_out.stdout.strip().split():
            logger.info(f"Scaling down operator deployment '{dep}'")
            subprocess.run(
                f"kubectl scale deployment {dep} -n {self.spec.operator_namespace} --replicas=0",
                shell=True, capture_output=True,
            )
        time.sleep(5)

        # Patch every StatefulSet in the cluster namespace.
        base_image = self.spec.resolved_base_image(self.version)
        sts_out = subprocess.run(
            f"kubectl get statefulsets -n {self.namespace} --no-headers "
            f"-o jsonpath='{{range .items[*]}}{{.metadata.name}} {{end}}' 2>/dev/null",
            shell=True, capture_output=True, text=True,
        )
        for sts in sts_out.stdout.strip().split():
            self._patch_statefulset(sts, base_image, image_tag)

    def _patch_statefulset(self, sts_name: str, base_image: str, image_tag: str):
        """Reset partition to 0 and replace base-image containers with image_tag."""
        # Find containers whose image matches the base image (exact or prefix).
        base_repo = base_image.split(":")[0]
        ctrs_out = subprocess.run(
            f"kubectl get statefulset {sts_name} -n {self.namespace} "
            f"-o jsonpath='{{range .spec.template.spec.containers[*]}}{{.name}} {{.image}}|{{end}}' "
            f"2>/dev/null",
            shell=True, capture_output=True, text=True,
        )
        containers_to_patch = []
        for entry in ctrs_out.stdout.strip().split("|"):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(" ", 1)
            if len(parts) == 2:
                ctr_name, ctr_image = parts
                if base_repo in ctr_image or ctr_image == base_image:
                    containers_to_patch.append({"name": ctr_name, "image": image_tag})

        if not containers_to_patch:
            logger.warning(
                f"No containers matching '{base_repo}' found in StatefulSet '{sts_name}' — "
                "skipping image patch, resetting partition only"
            )

        patch = {"spec": {"updateStrategy": {"rollingUpdate": {"partition": 0}}}}
        if containers_to_patch:
            patch["spec"]["template"] = {"spec": {"containers": containers_to_patch}}  # type: ignore[index]

        logger.info(
            f"Patching StatefulSet '{sts_name}': "
            f"partition=0, containers={[c['name'] for c in containers_to_patch]}"
        )
        subprocess.run(
            f"kubectl patch statefulset {sts_name} -n {self.namespace} "
            f"--type=strategic -p '{json.dumps(patch)}'",
            shell=True, check=True,
        )

    @staticmethod
    def _delete_namespace(namespace: str):
        subprocess.run(
            f"kubectl delete namespace {namespace} --ignore-not-found",
            shell=True, check=False,
        )
