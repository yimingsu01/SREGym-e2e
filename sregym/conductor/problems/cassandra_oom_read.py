"""Cassandra OOM: unbounded diagnostic buffer in ReadCommand causes heap exhaustion.

Bug: ReadCommand.executeLocally() allocates a 256 KB byte array into a static
     List<byte[]> (queryDiagnosticBuffer) on every local read.  The list has no
     eviction policy, so it grows without bound.  Under any sustained read workload
     the JVM heap fills up and Cassandra crashes with:

         java.lang.OutOfMemoryError: Java heap space

     Kubernetes OOMKills the container and restarts it.  The workload pod is still
     running, so reads resume immediately — the heap fills again within seconds —
     and the restart loop escalates to CrashLoopBackOff.

Root cause: ReadCommand.java — a diagnostic snapshot buffer accumulated in a
     static field was never removed before the release build.  The allocation
     (262,144 bytes × every local read) is the sole source of heap growth; no
     other change to Cassandra is needed to reproduce the crash.

Fix: remove the queryDiagnosticBuffer field and its single call-site in
     executeLocally() (ReadCommand.java).
"""

import logging
import subprocess
from pathlib import Path

from sregym.conductor.problems.cassandra_custom_build import CassandraCustomBuildProblem
from sregym.service.apps.cassandra import Cassandra
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_SETUP_CQL = """
    CREATE KEYSPACE IF NOT EXISTS bench_ks
        WITH REPLICATION = {'class': 'SimpleStrategy', 'replication_factor': 3};

    USE bench_ks;

    CREATE TABLE IF NOT EXISTS events (
        id  INT PRIMARY KEY,
        val TEXT
    );
"""

# Seed a handful of rows so reads actually hit storage paths.
_SEED_CQL = "\n".join(
    f"INSERT INTO bench_ks.events (id, val) VALUES ({i}, 'data_{i}');"
    for i in range(20)
)

_READ_CQL = "SELECT * FROM bench_ks.events;"


class _CassandraWithOomKill(Cassandra):
    """Cassandra with a small heap and OOM-kill JVM options pre-configured.

    Deployed with the clean upstream image.  The buggy image is swapped in at
    inject_fault() time via CassandraCustomBuildProblem._apply_buggy_image().
    """

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
    serverImage: "{self._mgmt_api_image(self.cassandra_version)}"
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
            additionalJvm11ServerOptions:
              - "-XX:OnOutOfMemoryError=/usr/local/bin/oom-kill-mgmt.sh"
        podTemplateSpec:
          spec:
            containers:
              - name: server-system-logger
                image: "{self._system_logger_image()}"
"""


class CassandraOomRead(CassandraCustomBuildProblem):
    """Cassandra crashes with OOM due to unbounded static diagnostic buffer in read path.

    Fault injection:
      1. Deploys Cassandra 4.1.7 built from a patched source where
         ReadCommand.executeLocally() accumulates 256 KB per read into a
         static List<byte[]> that is never cleared.
      2. Seeds the table with 20 rows.
      3. Deploys a reader Deployment that issues full-table scans in a tight
         loop.  Each scan executes 20 local reads → 5 MB of heap growth per
         loop iteration.  With a 512 MB JVM heap the node OOMs in under a
         minute, is OOMKilled by Kubernetes, restarts, and immediately OOMs
         again → CrashLoopBackOff.

    The agent observes OOMKilled Cassandra pods, finds OutOfMemoryError in the
    logs, and must locate the rogue static buffer in ReadCommand.java.
    """

    cassandra_version = "4.1.7"
    source_git_ref = "cassandra-4.1.7"
    patch_dir = Path(__file__).parent / "patches" / "cassandra_oom_read"

    root_cause_file = "src/java/org/apache/cassandra/db/ReadCommand.java"
    root_cause_description = (
        "ReadCommand.executeLocally() (ReadCommand.java) allocates a 256 KB byte "
        "array into a static, unbounded List<byte[]> (queryDiagnosticBuffer) on "
        "every local read execution.  The list has no maximum size and is never "
        "cleared, so heap usage grows monotonically with query volume.  Under any "
        "sustained read workload the JVM exhausts its heap and crashes with "
        "OutOfMemoryError: Java heap space.  Kubernetes OOMKills the pod and "
        "restarts it; the workload immediately resumes reads, the heap fills again, "
        "and the node enters CrashLoopBackOff.  Fix: remove the queryDiagnosticBuffer "
        "field and its allocation in executeLocally()."
    )

    trigger_cql = _SETUP_CQL

    def _create_app(self) -> Cassandra:
        return _CassandraWithOomKill(cassandra_version=self.cassandra_version)

    @mark_fault_injected
    def inject_fault(self):
        self._apply_buggy_image()

        logger.info("[CassandraOomRead] Setting up bench keyspace and seeding rows")
        self.app.run_cql(_SETUP_CQL)
        self.app.run_cql(_SEED_CQL)

        logger.info("[CassandraOomRead] Deploying read-loop workload — will OOM Cassandra nodes")
        self._deploy_reader()

    def _deploy_reader(self):
        cass_host = (
            f"{self.app.cluster_name}-{self.app.datacenter_name}-service"
            f".{self.namespace}.svc.cluster.local"
        )
        secret_name = f"{self.app.cluster_name}-superuser"

        # Store the script in a ConfigMap so YAML quoting issues don't corrupt it.
        reader_script = """\
import time, os
from cassandra.cluster import Cluster
from cassandra.auth import PlainTextAuthProvider

host  = os.environ['CASS_HOST']
user  = os.environ['CASS_USER']
passw = os.environ['CASS_PASS']
auth  = PlainTextAuthProvider(username=user, password=passw)

print('Connecting to', host, flush=True)
cluster = None
while cluster is None:
    try:
        cluster = Cluster([host], auth_provider=auth, connect_timeout=30)
        session = cluster.connect()
        session.execute('SELECT now() FROM system.local')
    except Exception as e:
        print('Not ready:', e, flush=True)
        cluster = None
        time.sleep(5)

print('Connected. Hammering reads...', flush=True)
n = 0
while True:
    try:
        list(session.execute('SELECT * FROM bench_ks.events'))
        n += 1
        if n % 500 == 0:
            print(f'{n} scans', flush=True)
    except Exception as e:
        print('Error:', e, flush=True)
        time.sleep(1)
"""

        configmap = f"""\
apiVersion: v1
kind: ConfigMap
metadata:
  name: cassandra-reader-script
  namespace: {self.namespace}
data:
  reader.py: |
{chr(10).join("    " + line for line in reader_script.splitlines())}
"""

        manifest = f"""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cassandra-reader
  namespace: {self.namespace}
  labels:
    app: cassandra-reader
spec:
  replicas: 1
  selector:
    matchLabels:
      app: cassandra-reader
  template:
    metadata:
      labels:
        app: cassandra-reader
    spec:
      containers:
      - name: reader
        image: python:3.11-slim
        env:
        - name: CASS_HOST
          value: "{cass_host}"
        - name: CASS_USER
          valueFrom:
            secretKeyRef:
              name: {secret_name}
              key: username
        - name: CASS_PASS
          valueFrom:
            secretKeyRef:
              name: {secret_name}
              key: password
        command: ["/bin/bash", "-c"]
        args: ["pip install cassandra-driver -q && python3 /reader.py"]
        volumeMounts:
        - name: script
          mountPath: /reader.py
          subPath: reader.py
      volumes:
      - name: script
        configMap:
          name: cassandra-reader-script
"""
        # Deploy ConfigMap first, then the Deployment
        for name, yaml in [("ConfigMap", configmap), ("Deployment", manifest)]:
            result = subprocess.run(
                "kubectl apply -f -",
                shell=True, input=yaml, capture_output=True, text=True,
            )
            if result.returncode != 0:
                logger.warning(f"[CassandraOomRead] Reader {name} deploy failed: {result.stderr.strip()}")
                return
        logger.info("[CassandraOomRead] Read-loop workload deployed")

    @mark_fault_injected
    def recover_fault(self):
        subprocess.run(
            f"kubectl delete deployment cassandra-reader -n {self.namespace} --ignore-not-found"
            f" && kubectl delete configmap cassandra-reader-script -n {self.namespace} --ignore-not-found",
            shell=True, check=False,
        )
        logger.info("[CassandraOomRead] Reader workload deleted")
