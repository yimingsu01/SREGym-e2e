"""Cassandra application deployed via the K8ssandra operator (cass-operator)."""

import json
import logging
import subprocess
import time

import yaml

from sregym.paths import BASE_DIR
from sregym.service.apps.base import Application
from sregym.service.cassandra_build_manager import CassandraBuildManager
from sregym.service.helm import Helm
from sregym.service.kubectl import KubeCtl

logger = logging.getLogger("all.application")

CASSANDRA_METADATA = BASE_DIR / "service" / "metadata" / "cassandra.json"


def _run(cmd: str, check: bool = True, input: str | None = None) -> str:
    """Run a shell command, raise on failure, return stdout."""
    result = subprocess.run(
        cmd,
        shell=True,
        check=False,
        capture_output=True,
        text=True,
        input=input,
    )
    if result.stdout:
        logger.debug(result.stdout.strip())
    if result.stderr:
        logger.debug(result.stderr.strip())
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed (exit {result.returncode}): {cmd}\n"
            f"stdout: {result.stdout.strip()}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return result.stdout


class Cassandra(Application):
    def __init__(self, cassandra_version: str | None = None):
        super().__init__(CASSANDRA_METADATA)
        self.kubectl = KubeCtl()
        self.load_app_json()

        with open(CASSANDRA_METADATA) as f:
            metadata = json.load(f)

        k8s_cfg = metadata["K8ssandra Config"]
        self.app_name = metadata["Name"]
        self.description = metadata["Desc"]
        self.operator_namespace = k8s_cfg["operator_namespace"]
        self.operator_helm_repo = k8s_cfg["operator_helm_repo"]
        self.operator_helm_repo_url = k8s_cfg["operator_helm_repo_url"]
        self.operator_chart = k8s_cfg["operator_chart"]
        self.cluster_name = k8s_cfg["cluster_name"]
        self.datacenter_name = k8s_cfg["datacenter_name"]
        self.cluster_size = k8s_cfg["cluster_size"]
        self.cassandra_version = cassandra_version or k8s_cfg["cassandra_version"]

        # The K8ssandraCluster CR must live in the operator's namespace (RBAC is namespace-scoped).
        # Pods are created in the same namespace as the CR, so we override self.namespace here.
        self.namespace = self.operator_namespace

    def deploy(self):
        """Deploy Cassandra cluster via K8ssandra operator."""
        logger.info(f"Deploying Cassandra {self.cassandra_version} via K8ssandra operator")

        self._ensure_cert_manager()
        self._install_operator()
        self._deploy_cassandra_cluster()
        self._wait_for_cluster_ready()

        logger.info("Cassandra cluster deployed and ready")

    def _ensure_cert_manager(self):
        """Install cert-manager if not already present (required by K8ssandra webhooks)."""
        result = subprocess.run(
            "kubectl get namespace cert-manager --ignore-not-found",
            shell=True, capture_output=True, text=True,
        )
        if "cert-manager" in result.stdout:
            logger.info("cert-manager already installed")
            return

        logger.info("Installing cert-manager (required by K8ssandra operator)...")
        _run("kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml")
        _run("kubectl wait --for=condition=Available deployment --all -n cert-manager --timeout=120s")
        # Wait for all cert-manager pods to be fully Ready (containers initialized).
        # The webhook TLS certificate is issued asynchronously after the controller
        # pod starts — pod Ready is a stronger signal than deployment Available.
        _run("kubectl wait pod --all -n cert-manager --for=condition=Ready --timeout=120s")
        logger.info("cert-manager ready")

    def _install_operator(self):
        """Install the K8ssandra operator via Helm (raises on failure)."""
        logger.info(f"Installing K8ssandra operator in namespace '{self.operator_namespace}'")

        try:
            Helm.add_repo(self.operator_helm_repo, self.operator_helm_repo_url)
            Helm.repo_update()
        except RuntimeError as e:
            logger.warning(f"Helm repo setup issue (continuing with cached charts): {e}")

        # Uninstall only if the release is in a failed state; otherwise always
        # run helm upgrade --install (idempotent) so that --set overrides such
        # as the system-logger image tag are applied even on subsequent runs.
        existing = subprocess.run(
            f"helm status k8ssandra-operator -n {self.operator_namespace}",
            shell=True, capture_output=True, text=True,
        )
        if existing.returncode == 0 and "failed" in existing.stdout:
            logger.warning("K8ssandra operator release is in 'failed' state — uninstalling before reinstall")
            subprocess.run(
                f"helm uninstall k8ssandra-operator -n {self.operator_namespace}",
                shell=True, check=False,
            )

        logger.info("Running helm upgrade --install for k8ssandra-operator...")
        # Retry to handle the cert-manager webhook TLS bootstrapping race: the
        # webhook pod may report Ready before its serving certificate is issued.
        helm_cmd = (
            f"helm upgrade --install k8ssandra-operator {self.operator_chart} "
            f"--namespace {self.operator_namespace} "
            f"--create-namespace "
            f"--set global.clusterScoped=true "
            # Pin system-logger to a glibc 2.28 (UBI8) build so it runs on
            # CPUs that lack x86-64-v3 (AVX2/FMA).  v1.30.0+ use a newer glibc
            # that performs a CPU feature check and crashes on older hardware.
            f"--set cassandraOperator.imageConfig.images.system-logger.tag=v1.22.0 "
            f"--wait --timeout 5m"
        )
        for attempt in range(1, 4):
            try:
                _run(helm_cmd)
                break
            except RuntimeError as e:
                if attempt == 3 or "webhook" not in str(e):
                    raise
                logger.warning(
                    f"k8ssandra-operator install failed (attempt {attempt}/3) — "
                    f"cert-manager webhook not ready yet, retrying in 20s"
                )
                time.sleep(20)
        logger.info("K8ssandra operator installed and ready")
        self._pin_system_logger_image()

    def _pin_system_logger_image(self):
        """Patch the cass-operator image ConfigMap to pin system-logger to a CPU-safe tag.

        The operator reads ``data.image_config.yaml`` inside the ConfigMap — a
        YAML string whose ``images.system-logger.tag`` field controls which sidecar
        image is injected into every Cassandra pod.  We update that field in-place
        and restart the operator so the change is loaded before the first
        CassandraDatacenter is reconciled.
        """
        sl_image = CassandraBuildManager.system_logger_image()
        target_tag = sl_image.split(":")[-1]
        cm_name = "k8ssandra-operator-cass-operator-image-config"

        result = subprocess.run(
            ["kubectl", "get", "configmap", cm_name,
             "-n", self.operator_namespace, "-o", "json"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.warning(
                f"[Cassandra] Could not read {cm_name} — "
                f"system-logger image may not be pinned: {result.stderr.strip()}"
            )
            return

        cm = json.loads(result.stdout)
        cfg_yaml = cm.get("data", {}).get("image_config.yaml", "")
        try:
            cfg = yaml.safe_load(cfg_yaml) or {}
            cfg.setdefault("images", {}).setdefault("system-logger", {})["tag"] = target_tag
            new_cfg_yaml = yaml.dump(cfg, default_flow_style=False)
        except Exception as exc:
            logger.warning(f"[Cassandra] Could not parse image_config.yaml: {exc} — skipping pin")
            return

        patch = json.dumps({"data": {"image_config.yaml": new_cfg_yaml}})
        r = subprocess.run(
            ["kubectl", "patch", "configmap", cm_name,
             "-n", self.operator_namespace, "--type=merge", f"-p={patch}"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            logger.warning(f"[Cassandra] ConfigMap patch failed: {r.stderr.strip()}")
            return
        logger.info(f"[Cassandra] system-logger pinned to {target_tag} in {cm_name}")

        # Restart the operator so it reads the updated ConfigMap before
        # reconciling any CassandraDatacenter.
        subprocess.run(
            ["kubectl", "rollout", "restart", "deployment/k8ssandra-operator",
             "-n", self.operator_namespace],
            capture_output=True, text=True,
        )
        subprocess.run(
            ["kubectl", "rollout", "status", "deployment/k8ssandra-operator",
             "-n", self.operator_namespace, "--timeout=120s"],
            capture_output=True, text=True,
        )
        logger.info("[Cassandra] k8ssandra-operator restarted with updated system-logger image")

    def _patch_system_logger_in_datacenter(self):
        """Patch CassandraDatacenter.spec.podTemplateSpec to override the system-logger image.

        The CassandraDatacenter CR (cass-operator's API) supports podTemplateSpec for
        per-container image overrides that survive reconciliation.  The K8ssandraCluster
        CR does NOT expose this field, so we patch the child CassandraDatacenter directly
        after the operator has created it.

        This handles both fresh deployments and re-runs against an already-running cluster
        where the Helm --set and ConfigMap patch alone are insufficient to update live pods.
        """
        image = CassandraBuildManager.system_logger_image()
        logger.info(
            f"[Cassandra] Patching CassandraDatacenter '{self.datacenter_name}' "
            f"to use system-logger {image}"
        )

        # Wait up to 90 s for the operator to create the CassandraDatacenter CR.
        deadline = time.time() + 90
        found = False
        while time.time() < deadline:
            r = subprocess.run(
                ["kubectl", "get", "cassandradatacenter", self.datacenter_name,
                 "-n", self.namespace, "--ignore-not-found"],
                capture_output=True, text=True,
            )
            if self.datacenter_name in r.stdout:
                found = True
                break
            time.sleep(5)

        if not found:
            logger.warning(
                f"[Cassandra] CassandraDatacenter '{self.datacenter_name}' not found "
                "after 90 s — skipping system-logger patch"
            )
            return

        patch = json.dumps({
            "spec": {
                "podTemplateSpec": {
                    "spec": {
                        "containers": [
                            {"name": "server-system-logger", "image": image}
                        ]
                    }
                }
            }
        })
        r = subprocess.run(
            ["kubectl", "patch", "cassandradatacenter", self.datacenter_name,
             "-n", self.namespace, "--type=merge", f"-p={patch}"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            logger.warning(
                f"[Cassandra] CassandraDatacenter podTemplateSpec patch failed: {r.stderr.strip()}"
            )
        else:
            logger.info(
                f"[Cassandra] system-logger image overridden to {image} in "
                f"CassandraDatacenter '{self.datacenter_name}'"
            )

    def _ensure_cluster_cr_gone(self) -> None:
        """Force-remove any lingering K8ssandraCluster CR (including Terminating ones).

        The operator can re-add its finalizer between our patch and delete during
        cleanup, leaving the CR stuck in Terminating.  We must clear it before
        applying a fresh CR with the same name or the API server will try to patch
        the terminating object and reject unknown fields.
        """
        r = subprocess.run(
            f"kubectl get k8ssandracluster {self.cluster_name} -n {self.namespace} --ignore-not-found",
            shell=True, capture_output=True, text=True,
        )
        if self.cluster_name not in r.stdout:
            return
        logger.info(f"[Cassandra] Stale K8ssandraCluster '{self.cluster_name}' found — force-removing before deploy")
        subprocess.run(
            f"kubectl patch k8ssandracluster/{self.cluster_name} -n {self.namespace} "
            f"--type=merge -p '{{\"metadata\":{{\"finalizers\":[]}}}}'",
            shell=True, capture_output=True,
        )
        subprocess.run(
            f"kubectl delete k8ssandracluster/{self.cluster_name} -n {self.namespace} "
            f"--ignore-not-found --grace-period=0 --wait=false",
            shell=True, capture_output=True,
        )
        subprocess.run(
            f"kubectl wait --for=delete k8ssandracluster/{self.cluster_name} -n {self.namespace} --timeout=30s",
            shell=True, capture_output=True,
        )

    def _deploy_cassandra_cluster(self):
        """Deploy the K8ssandraCluster CR using kubectl apply via stdin."""
        logger.info(f"Deploying Cassandra cluster '{self.cluster_name}' with {self.cluster_size} nodes")

        _run(
            f"kubectl create namespace {self.namespace} --dry-run=client -o yaml | kubectl apply -f -"
        )

        # Ensure no stale CR (e.g. stuck in Terminating) exists before applying.
        self._ensure_cluster_cr_gone()

        manifest = self._build_cluster_manifest()
        _run("kubectl apply -f -", input=manifest)
        logger.info(f"K8ssandraCluster CR applied to namespace '{self.namespace}'")

    def _storage_class(self) -> str:
        """Return the appropriate StorageClass for the current cluster.

        kind clusters ship with the 'standard' StorageClass backed by the
        local-path-provisioner.  Remote clusters use OpenEBS hostpath.
        """
        return "standard" if self.kubectl.is_emulated_cluster() else "openebs-hostpath"

    @staticmethod
    def _mgmt_api_image(version: str) -> str:
        """Return the k8ssandra management API image for the cluster's architecture.

        Delegates to ``CassandraBuildManager.mgmt_api_image`` — the single
        source of truth for arch detection and image selection.
        """
        return CassandraBuildManager.mgmt_api_image(version)

    @staticmethod
    def _system_logger_image() -> str:
        """Return the system-logger sidecar image safe for all CPUs.

        Delegates to ``CassandraBuildManager.system_logger_image`` — the single
        source of truth; avoids hardcoding the tag in every manifest.
        """
        return CassandraBuildManager.system_logger_image()

    def _system_logger_image(self) -> str:
        """CPU-safe system-logger sidecar image (UBI8 / glibc 2.28, no x86-64-v3 requirement)."""
        return CassandraBuildManager.system_logger_image()

    def _build_cluster_manifest(self) -> str:
        """Build the K8ssandraCluster custom resource YAML."""
        server_image = self._mgmt_api_image(self.cassandra_version)
        system_logger = self._system_logger_image()
        return f"""\
apiVersion: k8ssandra.io/v1alpha1
kind: K8ssandraCluster
metadata:
  name: {self.cluster_name}
  namespace: {self.namespace}
spec:
  cassandra:
    serverVersion: "{self.cassandra_version}"
    serverImage: "{server_image}"
    datacenters:
      - metadata:
          name: {self.datacenter_name}
        size: {self.cluster_size}
        storageConfig:
          cassandraDataVolumeClaimSpec:
            storageClassName: {self._storage_class()}
            accessModes:
              - ReadWriteOnce
            resources:
              requests:
                storage: 5Gi
        resources:
          requests:
            memory: 1Gi
            cpu: 500m
          limits:
            memory: 2Gi
            cpu: "1"
        config:
          jvmOptions:
            heapSize: 512M
        podTemplateSpec:
          spec:
            containers:
              - name: server-system-logger
                image: "{system_logger}"
"""

    def _nudge_operator(self) -> None:
        """Annotate the CassandraDatacenter to kick the cass-operator out of backoff.

        controller-runtime's workqueue uses exponential backoff capped at ~1000 s.
        After a storm of rapid reconcile errors (e.g. "pod not found" during
        StatefulSet initialization) the operator can sit silent for ~17 minutes.
        Writing any annotation to the resource queues an immediate reconcile,
        resetting the backoff.
        """
        import time as _time
        subprocess.run(
            f"kubectl annotate cassandradatacenter {self.datacenter_name} "
            f"-n {self.namespace} "
            f"force-reconcile=\"{int(_time.time())}\" --overwrite",
            shell=True, capture_output=True, text=True,
        )
        logger.info(f"[Cassandra] Nudged cass-operator (force-reconcile annotation) on '{self.datacenter_name}'")

    def _wait_for_cluster_ready(self):
        """Wait for the CassandraDatacenter Ready condition.

        K8ssandra starts Cassandra with -Dcassandra.skip_default_role_setup=true so
        there are no default users. The cass-operator creates the superuser as part
        of its reconciliation loop and only sets CassandraDatacenter Ready=True once
        that step completes. Pod readiness (2/2) fires several minutes earlier and is
        not a sufficient signal — waiting for it causes auth failures.

        The cass-operator uses controller-runtime's exponential backoff, which caps
        at ~1000 s (~17 min).  If a storm of reconcile errors fires during StatefulSet
        initialization the operator goes silent for the full backoff window.  We nudge
        it every 60 s to prevent that stall.
        """
        logger.info(f"Waiting for CassandraDatacenter '{self.datacenter_name}' Ready condition (up to 20 min)...")
        deadline = time.time() + 1200
        last_nudge = time.time()
        nudge_interval = 60  # seconds between operator nudges

        while time.time() < deadline:
            result = subprocess.run(
                f"kubectl get cassandradatacenter {self.datacenter_name} -n {self.namespace} "
                f"-o jsonpath='{{range .status.conditions[*]}}{{.type}}={{.status}}\\n{{end}}'",
                shell=True, capture_output=True, text=True,
            )
            conditions = dict(
                line.split("=", 1)
                for line in result.stdout.strip().split("\\n")
                if "=" in line
            )
            if conditions.get("Ready") == "True":
                logger.info("CassandraDatacenter Ready=True — superuser created, waiting for all pods to be 2/2...")
                # Ready=True means the superuser exists, but a concurrent rolling
                # restart (e.g. from _patch_system_logger_in_datacenter) may still
                # be finishing on the last pod.  Wait for all Cassandra pods to
                # report Ready so the cluster is fully stable before we return.
                subprocess.run(
                    f"kubectl wait pod -n {self.namespace} -l app.kubernetes.io/name=cassandra "
                    f"--for=condition=Ready --timeout=300s",
                    shell=True, capture_output=True, text=True,
                )
                logger.info("All Cassandra pods Ready — cluster fully ready")
                return

            now = time.time()
            if now - last_nudge >= nudge_interval:
                self._nudge_operator()
                last_nudge = now

            logger.debug(f"Datacenter conditions: {conditions} — retrying in 15s")
            time.sleep(15)

        raise RuntimeError(f"Timeout waiting for CassandraDatacenter '{self.datacenter_name}' to be Ready")

    def _get_cql_credentials(self) -> tuple[str, str]:
        """Retrieve the superuser credentials from the K8ssandra-managed secret."""
        import base64
        secret_name = f"{self.cluster_name}-superuser"
        username = subprocess.run(
            f"kubectl get secret {secret_name} -n {self.namespace} -o jsonpath='{{.data.username}}'",
            shell=True, capture_output=True, text=True,
        ).stdout.strip()
        password = subprocess.run(
            f"kubectl get secret {secret_name} -n {self.namespace} -o jsonpath='{{.data.password}}'",
            shell=True, capture_output=True, text=True,
        ).stdout.strip()
        return base64.b64decode(username).decode(), base64.b64decode(password).decode()

    def run_cql(self, cql: str) -> str:
        """Execute CQL statement(s) against the cluster via cqlsh.

        Pipes CQL via stdin so multi-statement scripts work correctly.
        Credentials are base64-encoded before being embedded in the shell
        command so special characters (quotes, spaces, etc.) never break
        the shell quoting.
        """
        import base64 as _b64

        pod = subprocess.run(
            f"kubectl get pods -n {self.namespace} -l app.kubernetes.io/name=cassandra "
            f"-o jsonpath='{{.items[0].metadata.name}}'",
            shell=True, capture_output=True, text=True,
        ).stdout.strip().strip("'")

        if not pod:
            raise RuntimeError(f"No Cassandra pods found in namespace '{self.namespace}'")

        username, password = self._get_cql_credentials()
        u_b64 = _b64.b64encode(username.encode()).decode()
        p_b64 = _b64.b64encode(password.encode()).decode()

        result = subprocess.run(
            f"kubectl exec -i -n {self.namespace} {pod} -c cassandra -- "
            f"bash -c '"
            f"U=$(echo {u_b64} | base64 -d); "
            f"P=$(echo {p_b64} | base64 -d); "
            f"cqlsh -u \"$U\" -p \"$P\" --request-timeout=30"
            f"'",
            shell=True, capture_output=True, text=True, input=cql,
        )
        logger.info(f"CQL stdout: {result.stdout}")
        if result.stderr:
            logger.info(f"CQL stderr: {result.stderr}")
        if result.returncode != 0:
            raise RuntimeError(f"CQL execution failed:\nstdout: {result.stdout}\nstderr: {result.stderr}")
        return result.stdout

    def update_server_image(self, new_image: str):
        """Patch the K8ssandraCluster to use a new server image and wait for rolling restart.

        Called after the agent rebuilds Cassandra from modified source.
        The patch adds (or replaces) ``spec.cassandra.serverImage`` in the CR;
        K8ssandra performs a rolling restart so pods pick up the new image.
        """
        import json as _json
        patch = _json.dumps({"spec": {"cassandra": {"serverImage": new_image}}})
        logger.info(f"Patching K8ssandraCluster to use image: {new_image}")
        _run(
            f"kubectl patch k8ssandracluster {self.cluster_name} -n {self.namespace} "
            f"--type=merge -p '{patch}'"
        )
        logger.info("Image patched — waiting for rolling restart to complete")
        self._wait_for_cluster_ready()
        logger.info(f"Cluster ready with new image: {new_image}")

    @staticmethod
    def _force_clear_namespace(namespace: str) -> None:
        """Delete a namespace, forcibly clearing spec.finalizers if it gets stuck.

        Kubernetes namespaces can hang in Terminating indefinitely when CRDs are
        removed before their finalizer controllers run.  The fix is to call the
        /finalize subresource with an empty finalizers list, which tells the API
        server the namespace is safe to reclaim.
        """
        # --wait=false returns immediately; we handle waiting ourselves below.
        subprocess.run(
            f"kubectl delete namespace {namespace} --ignore-not-found --wait=false",
            shell=True, check=False,
        )

        deadline = time.time() + 60
        while time.time() < deadline:
            r = subprocess.run(
                f"kubectl get namespace {namespace} -o jsonpath='{{.status.phase}}'",
                shell=True, capture_output=True, text=True,
            )
            if r.returncode != 0 or not r.stdout.strip().strip("'"):
                return  # gone
            if r.stdout.strip().strip("'") != "Terminating":
                return

            # While waiting, proactively strip finalizers from any lingering CRs
            # so the namespace controller can finish content removal.
            for crd in ["k8ssandraclusters", "cassandradatacenters", "replicatedsecrets"]:
                items = subprocess.run(
                    f"kubectl get {crd} -n {namespace} -o name 2>/dev/null",
                    shell=True, capture_output=True, text=True,
                ).stdout.strip().splitlines()
                for item in items:
                    if item:
                        subprocess.run(
                            f"kubectl patch {item} -n {namespace} "
                            f"--type=merge -p '{{\"metadata\":{{\"finalizers\":[]}}}}' 2>/dev/null",
                            shell=True, capture_output=True,
                        )

            time.sleep(5)

        # Still stuck — use the finalize subresource to force removal.
        logger.info(f"[Cassandra] Namespace '{namespace}' stuck Terminating — force-clearing spec.finalizers")
        ns_json = subprocess.run(
            f"kubectl get namespace {namespace} -o json",
            shell=True, capture_output=True, text=True,
        ).stdout
        try:
            ns = json.loads(ns_json)
            ns.setdefault("spec", {})["finalizers"] = []
            subprocess.run(
                f"kubectl replace --raw /api/v1/namespaces/{namespace}/finalize -f -",
                shell=True, input=json.dumps(ns), capture_output=True, text=True,
            )
        except Exception as exc:
            logger.warning(f"[Cassandra] Force-finalize of '{namespace}' failed: {exc}")

    def delete(self):
        """Delete the Cassandra cluster CRs, stripping finalizers so deletion completes.

        K8ssandraCluster and CassandraDatacenter carry operator-managed finalizers.
        We clear them explicitly before issuing the delete so the objects disappear
        immediately without needing the operator to be running.
        """
        for resource in [
            f"k8ssandracluster/{self.cluster_name}",
            f"cassandradatacenter/{self.datacenter_name}",
        ]:
            # Clear finalizers first, then delete.
            subprocess.run(
                f"kubectl patch {resource} -n {self.namespace} "
                f"--type=merge -p '{{\"metadata\":{{\"finalizers\":[]}}}}'",
                shell=True, capture_output=True,
            )
            subprocess.run(
                f"kubectl delete {resource} -n {self.namespace} "
                f"--ignore-not-found --grace-period=0 --wait=false",
                shell=True, check=False,
            )

        # Wait up to 60 s for both CRs to vanish before namespace deletion.
        for resource in [
            f"k8ssandracluster/{self.cluster_name}",
            f"cassandradatacenter/{self.datacenter_name}",
        ]:
            subprocess.run(
                f"kubectl wait --for=delete {resource} -n {self.namespace} --timeout=60s",
                shell=True, capture_output=True,
            )

    def cleanup(self):
        """Full cleanup: cluster CRs, namespaces, PVs, operator."""
        logger.info("Cleaning up Cassandra deployment")

        self.delete()

        # Clean up any leftover PVs bound to this namespace before removing it.
        pvs_out = subprocess.run(
            f"kubectl get pv --no-headers | grep '{self.namespace}' || true",
            shell=True, capture_output=True, text=True,
        ).stdout
        for line in pvs_out.strip().splitlines():
            if line:
                pv_name = line.split()[0]
                subprocess.run(
                    f"kubectl patch pv {pv_name} -p '{{\"metadata\":{{\"finalizers\":null}}}}'",
                    shell=True, check=False,
                )
                subprocess.run(f"kubectl delete pv {pv_name} --ignore-not-found --wait=false", shell=True, check=False)

        subprocess.run(
            f"helm uninstall k8ssandra-operator -n {self.operator_namespace} 2>/dev/null || true",
            shell=True, check=False,
        )

        # Force-clear namespace — handles the Terminating deadlock automatically.
        self._force_clear_namespace(self.operator_namespace)

        logger.info("Cassandra cleanup complete")

    def start_workload(self):
        """No-op — workload is driven by the problem's fault trigger."""
        pass

    def create_workload(self, **kwargs):
        """No-op."""
        pass


class CassandraWithCustomImage(Cassandra):
    """Cassandra deployment using a locally-built custom image.

    Pass ``custom_image`` as the Docker image name:tag produced by
    CassandraBuildManager.  The K8ssandraCluster manifest gains a
    ``serverImage`` field so the operator pulls this image instead of
    the default upstream one.
    """

    def __init__(self, cassandra_version: str, custom_image: str):
        super().__init__(cassandra_version=cassandra_version)
        self.custom_image = custom_image

    def _build_cluster_manifest(self) -> str:
        return f"""\
apiVersion: k8ssandra.io/v1alpha1
kind: K8ssandraCluster
metadata:
  name: {self.cluster_name}
  namespace: {self.namespace}
spec:
  cassandra:
    serverVersion: "{self.cassandra_version}"
    serverImage: "{self.custom_image}"
    datacenters:
      - metadata:
          name: {self.datacenter_name}
        size: {self.cluster_size}
        storageConfig:
          cassandraDataVolumeClaimSpec:
            storageClassName: {self._storage_class()}
            accessModes:
              - ReadWriteOnce
            resources:
              requests:
                storage: 5Gi
        resources:
          requests:
            memory: 1Gi
            cpu: 500m
          limits:
            memory: 2Gi
            cpu: "1"
        config:
          jvmOptions:
            heapSize: 512M

"""
