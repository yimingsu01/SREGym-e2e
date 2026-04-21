"""Per-database build + deployment configuration.

Each DBBuildSpec describes four pipeline phases for one database type:

  1. Source   — where to clone source and how to map a version string to a git tag
  2. Build    — what Docker image to compile inside and what command to run
  3. Package  — where the compiled artifact lands and how to wrap it in a Docker image
  4. Deploy   — how to install the Kubernetes operator, generate the cluster CR manifest,
                and patch a live cluster to swap in a custom-built image (fault injection)

DB_REGISTRY maps a short name (e.g. "cassandra") to its DBBuildSpec so that
problem classes only need to declare ``db_name = "cassandra"``.
"""

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class DBBuildSpec:
    # ── Phase 1: Source ──────────────────────────────────────────────────────
    name: str
    repo_url: str
    # "owner/repo" on GitHub — used to match an issue URL to this spec.
    github_repo: str
    # Maps a bare version string to the git tag used in this repo.
    # e.g. "cassandra-{version}" → "cassandra-4.1.7"
    #      "v{version}"          → "v7.2.4"
    version_tag_pattern: str

    # ── Phase 2: Build ───────────────────────────────────────────────────────
    # Docker image that provides the right toolchain (JDK, GCC, Rust, …).
    build_image: str
    # Shell command run inside that image with the source tree as the working dir.
    build_cmd: str

    # ── Phase 3: Package ─────────────────────────────────────────────────────
    # Glob relative to the source root that matches the compiled artifact.
    artifact_glob: str
    # Base Docker image to extend.  May contain {version} which is substituted
    # with the bare version string (e.g. "4.1.7").
    base_image: str
    # Absolute path inside the base image where the artifact is copied.
    artifact_dest: str

    # ── Phase 4: Deploy ──────────────────────────────────────────────────────
    # Helm details for the Kubernetes operator that manages this database.
    operator_helm_repo: str
    operator_helm_repo_url: str
    operator_chart: str
    operator_namespace: str

    # Default cluster name used when deploying (can be overridden per problem).
    default_cluster_name: str

    # Lowercase singular CR kind as kubectl expects it
    # (e.g. "k8ssandracluster", "tidbcluster").
    cr_kind: str

    # Generates the Kubernetes CR manifest YAML for this database.
    # Called as: cluster_manifest_fn(cluster_name, namespace, version, custom_image)
    # custom_image is None for a stock deploy; set to an image tag for a buggy deploy.
    cluster_manifest_fn: Callable[[str, str, str, str | None], str]

    # Returns the JSON merge-patch dict that swaps the running cluster's image.
    # Called as: image_patch_fn(cluster_name, namespace, new_image) → dict
    image_patch_fn: Callable[[str, str, str], dict]

    # Optional operator prerequisites (e.g. cert-manager for K8ssandra).
    # Called once before the Helm operator install with no arguments.
    prereqs_fn: Callable[[], None] | None = field(default=None)

    # Jira project key for this database (e.g. "CASSANDRA", "ZOOKEEPER").
    # Set this so JiraIssueParser can map an issue URL to this spec.
    jira_project: str | None = field(default=None)

    # Runs a reproducer (SQL/shell/script string) against the live cluster once.
    # Called as: run_reproducer_fn(cluster_name, namespace, reproducer)
    run_reproducer_fn: Callable[[str, str, str], None] | None = field(default=None)

    # Returns a Kubernetes manifest (ConfigMap + Deployment) that continuously
    # runs the reproducer on the cluster so the bug stays observable.
    # Called as: reproducer_workload_fn(cluster_name, namespace, reproducer, expected_output) → str
    # expected_output: correct value for wrong-result bugs; None for error/crash bugs.
    reproducer_workload_fn: Callable[[str, str, str, str | None], str] | None = field(default=None)

    # Extra --set / --values flags appended to the Helm operator install command.
    operator_extra_helm_args: str = field(default="")

    # Skip source compile and produce a custom image that just re-tags the stock
    # base image.  Use this when the source tree cannot be compiled standalone
    # (e.g. MongoDB 8.0+ public repo unconditionally references a private
    # enterprise modules repo) but the bug is already present in the stock
    # binary.  build_cmd / artifact_glob / artifact_dest are ignored in this mode.
    prebuilt_from_stock: bool = field(default=False)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def git_ref(self, version: str) -> str:
        """Convert a bare version string to the git tag for this database."""
        return self.version_tag_pattern.format(version=version)

    def resolved_base_image(self, version: str) -> str:
        """Substitute {version} in base_image."""
        return self.base_image.format(version=version)

    def resolved_artifact_dest(self, version: str) -> str:
        """Substitute {version} in artifact_dest (some images have version in their paths)."""
        return self.artifact_dest.format(version=version)


# ── Per-DB functions ──────────────────────────────────────────────────────────

def _cassandra_image_patch(_cluster: str, _ns: str, image: str) -> dict:  # type: ignore[override]
    return {"spec": {"cassandra": {"serverImage": image}}}


def _cassandra_cluster_manifest(
    cluster_name: str,
    namespace: str,
    version: str,
    custom_image: str | None,
) -> str:
    server_image = f'\n    serverImage: "{custom_image}"' if custom_image else ""
    return f"""\
apiVersion: k8ssandra.io/v1alpha1
kind: K8ssandraCluster
metadata:
  name: {cluster_name}
  namespace: {namespace}
spec:
  cassandra:
    serverVersion: "{version}"{server_image}
    datacenters:
      - metadata:
          name: dc1
        size: 3
        storageConfig:
          cassandraDataVolumeClaimSpec:
            storageClassName: openebs-hostpath
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


def _ensure_cert_manager() -> None:
    """Install cert-manager if not already present (required by K8ssandra webhooks)."""
    import subprocess
    import logging
    log = logging.getLogger(__name__)

    result = subprocess.run(
        "kubectl get namespace cert-manager --ignore-not-found",
        shell=True, capture_output=True, text=True,
    )
    if "cert-manager" in result.stdout:
        return

    log.info("Installing cert-manager (required by K8ssandra operator)...")
    subprocess.run(
        "kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml",
        shell=True, check=True,
    )
    subprocess.run(
        "kubectl wait --for=condition=Available deployment --all -n cert-manager --timeout=120s",
        shell=True, check=True,
    )
    subprocess.run(
        "kubectl wait pod --all -n cert-manager --for=condition=Ready --timeout=120s",
        shell=True, check=True,
    )
    log.info("cert-manager ready")


def _tidb_image_patch(_cluster: str, _ns: str, image: str) -> dict:  # type: ignore[override]
    return {"spec": {"tidb": {"image": image}}}


def _tidb_cluster_manifest(
    cluster_name: str,
    namespace: str,
    version: str,
    custom_image: str | None,
) -> str:
    tidb_image = f'\n    image: "{custom_image}"' if custom_image else ""
    return f"""\
apiVersion: pingcap.com/v1alpha1
kind: TidbCluster
metadata:
  name: {cluster_name}
  namespace: {namespace}
spec:
  version: "v{version}"
  timezone: UTC
  pvReclaimPolicy: Delete
  enableDynamicConfiguration: true
  configUpdateStrategy: RollingUpdate
  discovery: {{}}
  helper:
    image: alpine:3.16.0
  pd:
    baseImage: pingcap/pd
    maxFailoverCount: 0
    replicas: 1
    storageClassName: openebs-hostpath
    requests:
      storage: 1Gi
    config: {{}}
  tikv:
    baseImage: pingcap/tikv
    maxFailoverCount: 0
    replicas: 1
    evictLeaderTimeout: 1m
    storageClassName: openebs-hostpath
    requests:
      storage: 1Gi
    config:
      storage:
        reserve-space: 0MB
      rocksdb:
        max-open-files: 256
      raftdb:
        max-open-files: 256
  tidb:
    baseImage: pingcap/tidb
    maxFailoverCount: 0
    replicas: 1{tidb_image}
    service:
      type: ClusterIP
    config: {{}}
"""


def _ensure_tidb_crds() -> None:
    """Install TiDB CRDs if not already present."""
    import subprocess
    import logging
    log = logging.getLogger(__name__)

    result = subprocess.run(
        "kubectl get crd tidbclusters.pingcap.com --ignore-not-found",
        shell=True, capture_output=True, text=True,
    )
    if "tidbclusters" in result.stdout:
        return

    log.info("Installing TiDB CRDs...")
    subprocess.run(
        "kubectl apply --server-side -f https://raw.githubusercontent.com/pingcap/tidb-operator/v1.6.0/manifests/crd.yaml",
        shell=True, check=True,
    )
    log.info("TiDB CRDs installed")


# ── Reproducer runners ───────────────────────────────────────────────────────

def _cassandra_run_reproducer(cluster_name: str, namespace: str, reproducer: str) -> None:
    import subprocess
    import logging
    log = logging.getLogger(__name__)
    svc = f"{cluster_name}-dc1-service.{namespace}.svc.cluster.local"
    pod = "cassandra-cql-client"

    log.info("[Reproducer] Running Cassandra CQL reproducer")
    try:
        subprocess.run(
            f"kubectl delete pod {pod} -n {namespace} --ignore-not-found",
            shell=True, capture_output=True,
        )
        subprocess.run(
            f"kubectl run {pod} --image=cassandra:4.1 --restart=Never -n {namespace} -- sleep 3600",
            shell=True, check=True, capture_output=True,
        )
        subprocess.run(
            f"kubectl wait pod/{pod} -n {namespace} --for=condition=Ready --timeout=120s",
            shell=True, check=True, capture_output=True,
        )
        result = subprocess.run(
            f"kubectl exec -i {pod} -n {namespace} -- cqlsh {svc}",
            shell=True, input=reproducer,
            capture_output=True, text=True, timeout=120,
        )
        stderr = result.stderr.strip()
        if result.returncode == 0:
            log.info(f"[Reproducer] Query completed: {result.stdout.strip()[:200]}")
        else:
            log.info(f"[Reproducer] cqlsh exited {result.returncode} (may be expected): {stderr[:300]}")
    except Exception as e:
        log.warning(f"[Reproducer] Error: {e}")
    finally:
        subprocess.run(
            f"kubectl delete pod {pod} -n {namespace} --ignore-not-found --wait=false",
            shell=True, capture_output=True,
        )


def _tidb_run_reproducer(cluster_name: str, namespace: str, reproducer: str) -> None:
    import subprocess
    import logging
    log = logging.getLogger(__name__)
    svc = f"{cluster_name}-tidb.{namespace}.svc.cluster.local"
    pod = "tidb-sql-client"

    # Wrap reproducer in a fresh temp database to avoid table-exists errors and
    # DROP DATABASE IF EXISTS raising ERROR 1008 on TiDB when DB doesn't exist.
    db_name = "sregym_oneshot"
    reproducer = (
        f"CREATE DATABASE IF NOT EXISTS `{db_name}`;\n"
        f"USE `{db_name}`;\n"
        + reproducer
    )

    log.info("[Reproducer] Running TiDB SQL reproducer")
    try:
        subprocess.run(
            f"kubectl delete pod {pod} -n {namespace} --ignore-not-found",
            shell=True, capture_output=True,
        )
        subprocess.run(
            f"kubectl run {pod} --image=mysql:8.0 --restart=Never -n {namespace} -- sleep 3600",
            shell=True, check=True, capture_output=True,
        )
        subprocess.run(
            f"kubectl wait pod/{pod} -n {namespace} --for=condition=Ready --timeout=120s",
            shell=True, check=True, capture_output=True,
        )
        result = subprocess.run(
            f"kubectl exec -i {pod} -n {namespace} -- "
            f"mysql -h {svc} -P 4000 -u root --connect-timeout=15",
            shell=True, input=reproducer,
            capture_output=True, text=True, timeout=120,
        )
        stderr = result.stderr.strip()
        if result.returncode == 0:
            log.info(f"[Reproducer] Query completed: {result.stdout.strip()[:200]}")
        else:
            log.info(f"[Reproducer] mysql exited {result.returncode} (may be expected): {stderr[:300]}")
    except Exception as e:
        log.warning(f"[Reproducer] Error: {e}")
    finally:
        subprocess.run(
            f"kubectl delete pod {pod} -n {namespace} --ignore-not-found --wait=false",
            shell=True, capture_output=True,
        )


# ── Continuous reproducer workloads ──────────────────────────────────────────

def _workload_manifest(
    cluster_name: str,
    namespace: str,
    client_image: str,
    loop_cmd: str,
    script_content: str,
    script_filename: str = "run.script",
    probe_script: str | None = None,
) -> str:
    """Build a ConfigMap + Deployment manifest that runs script_content in a loop.

    probe_script: shell script stored as probe.sh in the ConfigMap and executed
    by the readiness probe.  It should exit 0 when the DB is reachable (even if
    the reproducer query fails with the expected bug error) and exit 1 when the
    DB cannot be reached.  Always invoked as /bin/sh /scripts/probe.sh so the
    probe command itself is fully generic.
    """
    indented = "\n".join("    " + l for l in script_content.splitlines())

    probe_entry = ""
    probe_yaml = ""
    if probe_script:
        probe_indented = "\n".join("    " + l for l in probe_script.splitlines())
        probe_entry = f"\n  probe.sh: |\n{probe_indented}"
        probe_yaml = """
        readinessProbe:
          exec:
            command:
              - /bin/sh
              - /scripts/probe.sh
          initialDelaySeconds: 15
          periodSeconds: 10
          failureThreshold: 3"""

    return f"""\
apiVersion: v1
kind: ConfigMap
metadata:
  name: {cluster_name}-reproducer
  namespace: {namespace}
data:
  {script_filename}: |
{indented}{probe_entry}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {cluster_name}-reproducer
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: reproducer
    app.kubernetes.io/instance: {cluster_name}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {cluster_name}-reproducer
  template:
    metadata:
      labels:
        app: {cluster_name}-reproducer
    spec:
      volumes:
      - name: script
        configMap:
          name: {cluster_name}-reproducer
      containers:
      - name: reproducer
        image: {client_image}
        volumeMounts:
        - name: script
          mountPath: /scripts
        command: ["/bin/sh", "-c"]
        args:
        - |
          {loop_cmd}{probe_yaml}
      restartPolicy: Always

"""


def _tidb_reproducer_workload(
    cluster_name: str, namespace: str, reproducer: str, expected_output: str | None = None
) -> str:
    # run.sql holds the raw reproducer; the shell loop prepends DB setup so we
    # never run DROP DATABASE (which raises ERROR 1008 on TiDB even with IF EXISTS).
    # A timestamped DB name avoids table-already-exists errors between iterations.
    svc = f"{cluster_name}-tidb.{namespace}.svc.cluster.local"
    mysql = f"mysql -h {svc} -P 4000 -u root --connect-timeout=15"
    loop_cmd = (
        "while true; do "
                'DB=sregym_$(date +%s); '
        # printf preamble → cat script → pipe to mysql; no backtick quoting needed
        # for sregym_<timestamp> identifiers
        f'out=$(printf "SET GLOBAL tidb_mem_quota_query=0;\\nSET SESSION max_execution_time=15000;\\nCREATE DATABASE $DB;\\nUSE $DB;\\n" | cat - /scripts/run.sql | timeout 30 {mysql} --table 2>&1); '
        'rc=$?; if [ -n "$out" ]; then echo "$out"; else echo "(empty result set)"; fi; '
        f'mysql -h {svc} -P 4000 -u root --connect-timeout=5 -e "DROP DATABASE $DB" > /dev/null 2>&1 || true; '
        "sleep 10; done"
    )
    if expected_output:
        safe = expected_output.replace("'", "'\\''")
        probe_script = (
            "#!/bin/sh\n"
            "DB=sregym_probe\n"
            f'mysql -h {svc} -P 4000 -u root --connect-timeout=5 -e "CREATE DATABASE IF NOT EXISTS $DB" > /dev/null 2>&1\n'
            f'out=$(printf "USE $DB;\\n" | cat - /scripts/run.sql | {mysql} --batch --skip-column-names 2>/dev/null)\n'
            f"printf '%s' \"$out\" | grep -qF '{safe}' && exit 0\n"
            "exit 1\n"
        )
    else:
        probe_script = (
            "#!/bin/sh\n"
            "DB=sregym_probe_$(date +%s)\n"
            f'printf "CREATE DATABASE $DB;\\nUSE $DB;\\n" | cat - /scripts/run.sql | {mysql} > /dev/null 2>&1\n'
            "rc=$?\n"
            f'mysql -h {svc} -P 4000 -u root --connect-timeout=5 -e "DROP DATABASE $DB" > /dev/null 2>&1 || true\n'
            "exit $rc\n"
        )
    return _workload_manifest(cluster_name, namespace, "mysql:8.0", loop_cmd, reproducer, "run.sql", probe_script)


def _mongodb_image_patch(_cluster: str, _ns: str, image: str) -> dict:  # type: ignore[override]
    return {
        "spec": {
            "statefulSet": {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [{"name": "mongod", "image": image}]
                        }
                    }
                }
            }
        }
    }


def _mongodb_cluster_manifest(
    cluster_name: str,
    namespace: str,
    version: str,
    custom_image: str | None,
) -> str:
    custom_image_override = (
        f"\n        spec:\n          containers:\n          - name: mongod\n            image: \"{custom_image}\""
        if custom_image else ""
    )
    return f"""\
apiVersion: v1
kind: Secret
metadata:
  name: mongodb-admin-password
  namespace: {namespace}
type: Opaque
stringData:
  password: sregym-test-pass
---
apiVersion: mongodbcommunity.mongodb.com/v1
kind: MongoDBCommunity
metadata:
  name: {cluster_name}
  namespace: {namespace}
spec:
  members: 1
  type: ReplicaSet
  version: "{version}"
  security:
    authentication:
      modes: ["SCRAM"]
  users:
    - name: admin
      db: admin
      passwordSecretRef:
        name: mongodb-admin-password
      roles:
        - name: clusterAdmin
          db: admin
        - name: userAdminAnyDatabase
          db: admin
        - name: readWriteAnyDatabase
          db: admin
      scramCredentialsSecretName: mongodb-admin-scram
  statefulSet:
    spec:
      template:
        metadata:
          labels:
            app.kubernetes.io/instance: {cluster_name}{custom_image_override}
"""


def _mongodb_run_reproducer(cluster_name: str, namespace: str, reproducer: str) -> None:
    import subprocess
    import logging
    log = logging.getLogger(__name__)
    svc = f"{cluster_name}-svc.{namespace}.svc.cluster.local"
    conn = (
        f"mongodb://admin:sregym-test-pass@{svc}:27017/admin"
        f"?authSource=admin&replicaSet={cluster_name}"
    )
    pod = "mongodb-mongosh-client"

    log.info("[Reproducer] Running MongoDB reproducer")
    try:
        subprocess.run(
            f"kubectl delete pod {pod} -n {namespace} --ignore-not-found",
            shell=True, capture_output=True,
        )
        subprocess.run(
            f"kubectl run {pod} --image=mongo:6 --restart=Never -n {namespace} -- sleep 3600",
            shell=True, check=True, capture_output=True,
        )
        subprocess.run(
            f"kubectl wait pod/{pod} -n {namespace} --for=condition=Ready --timeout=120s",
            shell=True, check=True, capture_output=True,
        )
        result = subprocess.run(
            f"kubectl exec -i {pod} -n {namespace} -- mongosh '{conn}' --quiet",
            shell=True, input=reproducer,
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            log.info(f"[Reproducer] Output: {result.stdout.strip()[:200]}")
        else:
            log.info(f"[Reproducer] mongosh exited {result.returncode}: {result.stderr.strip()[:300]}")
    except Exception as e:
        log.warning(f"[Reproducer] Error: {e}")
    finally:
        subprocess.run(
            f"kubectl delete pod {pod} -n {namespace} --ignore-not-found --wait=false",
            shell=True, capture_output=True,
        )


def _mongodb_reproducer_workload(
    cluster_name: str, namespace: str, reproducer: str, expected_output: str | None
) -> str:
    svc = f"{cluster_name}-svc.{namespace}.svc.cluster.local"
    conn = (
        f"mongodb://admin:sregym-test-pass@{svc}:27017/admin"
        f"?authSource=admin&replicaSet={cluster_name}"
    )
    loop_cmd = (
        "while true; do "
                f"out=$(mongosh '{conn}' --quiet < /scripts/run.js 2>&1); "
        'rc=$?; [ -n "$out" ] && echo "$out"; '
        "sleep 10; done"
    )
    if expected_output:
        safe = expected_output.replace("'", "'\\''")
        probe_script = (
            "#!/bin/sh\n"
            f"out=$(mongosh '{conn}' --quiet < /scripts/run.js 2>/dev/null)\n"
            f"printf '%s' \"$out\" | grep -qF '{safe}' && exit 0\n"
            "exit 1\n"
        )
    else:
        probe_script = (
            "#!/bin/sh\n"
            f"mongosh '{conn}' --quiet < /scripts/run.js > /dev/null 2>&1\n"
        )
    return _workload_manifest(cluster_name, namespace, "mongo:6", loop_cmd, reproducer, "run.js", probe_script)


def _cassandra_reproducer_workload(
    cluster_name: str, namespace: str, reproducer: str, expected_output: str | None = None
) -> str:
    svc = f"{cluster_name}-dc1-service.{namespace}.svc.cluster.local"
    loop_cmd = (
        "while true; do "
                f"out=$(cqlsh {svc} < /scripts/run.cql 2>&1); "
        'rc=$?; [ -n "$out" ] && echo "$out"; '
        "sleep 10; done"
    )
    if expected_output:
        safe = expected_output.replace("'", "'\\''")
        probe_script = (
            "#!/bin/sh\n"
            f"out=$(cqlsh {svc} --no-color < /scripts/run.cql 2>/dev/null)\n"
            f"printf '%s' \"$out\" | grep -qF '{safe}' && exit 0\n"
            "exit 1\n"
        )
    else:
        # Error/crash bug: Ready = query runs without error (bug fixed).
        # Not Ready = query errors (bug active) or DB unreachable.
        probe_script = (
            "#!/bin/sh\n"
            f"cqlsh {svc} < /scripts/run.cql > /dev/null 2>&1\n"
        )
    return _workload_manifest(cluster_name, namespace, "cassandra:4.1", loop_cmd, reproducer, "run.cql", probe_script)


# ── Registry ─────────────────────────────────────────────────────────────────

DB_REGISTRY: dict[str, DBBuildSpec] = {
    "cassandra": DBBuildSpec(
        name="cassandra",
        repo_url="https://github.com/apache/cassandra",
        github_repo="apache/cassandra",
        version_tag_pattern="cassandra-{version}",
        build_image="eclipse-temurin:11",
        build_cmd=(
            "apt-get update -qq && apt-get install -y -q ant && "
            "ant jar -Duse.jdk11=true"
        ),
        artifact_glob="build/apache-cassandra-*.jar",
        base_image="k8ssandra/cass-management-api:{version}-ubi8",
        artifact_dest="/opt/cassandra/lib/",
        operator_helm_repo="k8ssandra",
        operator_helm_repo_url="https://helm.k8ssandra.io/stable",
        operator_chart="k8ssandra/k8ssandra-operator",
        operator_namespace="k8ssandra-operator",
        default_cluster_name="sregym-cassandra",
        cr_kind="k8ssandracluster",
        cluster_manifest_fn=_cassandra_cluster_manifest,
        image_patch_fn=_cassandra_image_patch,
        prereqs_fn=_ensure_cert_manager,
        jira_project="CASSANDRA",
        run_reproducer_fn=_cassandra_run_reproducer,
        reproducer_workload_fn=_cassandra_reproducer_workload,
    ),
    "tidb": DBBuildSpec(
        name="tidb",
        repo_url="https://github.com/pingcap/tidb",
        github_repo="pingcap/tidb",
        version_tag_pattern="v{version}",
        build_image="golang:1.23",
        build_cmd="GOFLAGS=-buildvcs=false make server || make tidb-server || make build",
        artifact_glob="bin/tidb-server*",
        base_image="pingcap/tidb:v{version}",
        artifact_dest="/tidb-server",
        operator_helm_repo="pingcap",
        operator_helm_repo_url="https://charts.pingcap.org",
        operator_chart="pingcap/tidb-operator",
        operator_namespace="tidb-admin",
        default_cluster_name="sregym-tidb",
        cr_kind="tidbcluster",
        cluster_manifest_fn=_tidb_cluster_manifest,
        image_patch_fn=_tidb_image_patch,
        prereqs_fn=_ensure_tidb_crds,
        jira_project=None,
        run_reproducer_fn=_tidb_run_reproducer,
        reproducer_workload_fn=_tidb_reproducer_workload,
        # Pin chart to match the CRD manifest version — mixing CRD v1.6.0 with
        # chart v1.6.5 leaves compactbackups.pingcap.com missing, which stalls
        # the controller reconciliation loop and prevents any pods from starting.
        # Disable admission webhook and scheduler (both need cert infra on kind).
        operator_extra_helm_args=(
            "--version v1.6.0 "
            "--set admissionWebhook.create=false "
            "--set scheduler.create=false"
        ),
    ),
    "mongodb": DBBuildSpec(
        name="mongodb",
        repo_url="https://github.com/mongodb/mongo",
        github_repo="mongodb/mongo",
        # MongoDB release tags use "r" prefix: r7.0.5, r6.0.10, etc.
        version_tag_pattern="r{version}",
        # build_image / build_cmd / artifact_glob / artifact_dest are not used
        # when prebuilt_from_stock=True.  Kept as placeholders for the schema.
        build_image="ubuntu:22.04",
        build_cmd="true",
        artifact_glob="UNUSED",
        base_image="mongodb/mongodb-community-server:{version}-ubi8",
        artifact_dest="/usr/bin/mongod",
        # The mongo public source at 8.0+ cannot be built standalone:
        #   • src/BUILD.bazel:10 (core_headers_library) unconditionally references
        #     //src/mongo/db/modules/enterprise/... which lives in a private repo.
        #   • .bazelrc pins build_enterprise=True in every profile.
        #   • SCons is mid-migration and fails on murmurhash3/s2/snappy/tcmalloc.
        # Community jira bugs like SERVER-110803 exist in the published stock
        # image (e.g. mongodb/mongodb-community-server:8.0.13-ubi8), so the
        # "custom" image can simply wrap the stock one — the buggy binary is
        # already there.  The source tree is still cloned for static analysis
        # by diagnosis oracles.
        prebuilt_from_stock=True,
        operator_helm_repo="mongodb",
        operator_helm_repo_url="https://mongodb.github.io/helm-charts",
        operator_chart="mongodb/community-operator",
        operator_namespace="mongodb-operator",
        default_cluster_name="sregym-mongodb",
        cr_kind="mongodbcommunity",
        cluster_manifest_fn=_mongodb_cluster_manifest,
        image_patch_fn=_mongodb_image_patch,
        jira_project="SERVER",
        run_reproducer_fn=_mongodb_run_reproducer,
        reproducer_workload_fn=_mongodb_reproducer_workload,
    ),
}
