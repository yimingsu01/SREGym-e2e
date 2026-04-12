"""Cassandra OOM: unbounded write-coalesce cache in Mutation.apply() causes heap exhaustion.

Bug: Mutation.apply() maintains a static List<byte[]> (writeCoalesceCache) intended
     as a zero-copy forwarding buffer for speculative read-your-writes requests.
     Every write to bench_ks adds a new slot sized by Math.max(key + indexedWidth, 256 KB).
     Tables without secondary indexes always fall through to the 256 KB fallback, so
     every INSERT grows the list by 256 KB.  The list has no eviction policy and is
     never cleared, so heap usage grows linearly with write throughput.

     With a JVM heap of 512 MB and a writer issuing ~100 INSERTs/s, the heap fills
     in ~2 minutes:

         java.lang.OutOfMemoryError: Java heap space

     Kubernetes OOMKills the container and restarts it.  The writer pod reconnects
     immediately, resumes INSERTs, the heap fills again, and the restart loop
     escalates to CrashLoopBackOff.

Root cause: Mutation.java — writeCoalesceCache static field accumulates 256 KB per
     write to bench_ks and is never bounded or cleared.
     The Math.max(..., 1024 * 256) guard intended to prevent zero-size slots instead
     ensures every unindexed write allocates exactly 256 KB.

Fix: remove the writeCoalesceCache field and the population block in apply().
"""

import logging
import subprocess
from pathlib import Path

from sregym.conductor.problems.cassandra_custom_build import CassandraCustomBuildProblem
from sregym.service.apps.cassandra import Cassandra
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_SETUP_CQL = """\
    CREATE KEYSPACE IF NOT EXISTS bench_ks
        WITH REPLICATION = {'class': 'SimpleStrategy', 'replication_factor': 3};

    USE bench_ks;

    CREATE TABLE IF NOT EXISTS events (
        id  INT PRIMARY KEY,
        val TEXT
    );
"""

_SEED_CQL = "\n".join(
    f"INSERT INTO bench_ks.events (id, val) VALUES ({i}, 'seed_{i}');"
    for i in range(10)
)


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


class CassandraWriteCacheOom(CassandraCustomBuildProblem):
    """Cassandra crashes with OOM due to unbounded static write-coalesce cache in Mutation.apply().

    Fault injection:
      1. Deploys Cassandra 4.1.7 built from a patched source where
         Mutation.apply() accumulates 256 KB per write into a static
         List<byte[]> (writeCoalesceCache) that is never cleared.
      2. Creates keyspace bench_ks and seeds a few rows.
      3. Deploys a writer Deployment that issues INSERTs in a tight loop.
         Each INSERT → 256 KB heap growth.  With a 512 MB JVM heap, OOM
         occurs within ~2 minutes, Kubernetes OOMKills the pod, it restarts,
         the writer reconnects and resumes → CrashLoopBackOff.

    The agent observes OOMKilled Cassandra pods, finds OutOfMemoryError in the
    logs originating from the write path, and must locate the unbounded cache in
    Mutation.java and remove it.
    """

    cassandra_version = "4.1.7"
    source_git_ref = "cassandra-4.1.7"
    patch_dir = Path(__file__).parent / "patches" / "cassandra_write_cache_oom"

    root_cause_file = "src/java/org/apache/cassandra/db/Mutation.java"
    root_cause_description = (
        "Mutation.apply() in Mutation.java populates a static, unbounded "
        "List<byte[]> (writeCoalesceCache) on every write to bench_ks. "
        "The slot size is computed as Math.max(key.remaining() + indexedWidth, 1024 * 256). "
        "For tables without secondary indexes indexedWidth is 0, so the Math.max guard "
        "always allocates exactly 262,144 bytes (256 KB) per write. "
        "The list is never cleared or bounded, so heap usage grows at 256 KB × write rate. "
        "Under any sustained write workload the JVM exhausts its heap and crashes with "
        "OutOfMemoryError: Java heap space.  Kubernetes OOMKills the pod and restarts it; "
        "the writer resumes immediately, the heap fills again, and the node enters "
        "CrashLoopBackOff.  Fix: remove the writeCoalesceCache field and its population "
        "block in apply()."
    )

    trigger_cql = _SETUP_CQL

    def _create_app(self) -> Cassandra:
        return _CassandraWithOomKill(cassandra_version=self.cassandra_version)

    @mark_fault_injected
    def inject_fault(self):
        self._apply_buggy_image()

        logger.info("[CassandraWriteCacheOom] Setting up bench keyspace and seeding rows")
        self.app.run_cql(_SETUP_CQL)
        self.app.run_cql(_SEED_CQL)

        logger.info("[CassandraWriteCacheOom] Deploying writer workload — will OOM Cassandra nodes")
        self._deploy_writer()

    def _deploy_writer(self):
        cass_host = (
            f"{self.app.cluster_name}-{self.app.datacenter_name}-service"
            f".{self.namespace}.svc.cluster.local"
        )
        secret_name = f"{self.app.cluster_name}-superuser"

        writer_script = """\
import time, os, random, string
from cassandra.cluster import Cluster
from cassandra.auth import PlainTextAuthProvider

host  = os.environ['CASS_HOST']
user  = os.environ['CASS_USER']
passw = os.environ['CASS_PASS']
auth  = PlainTextAuthProvider(username=user, password=passw)

print('Connecting to', host, flush=True)
cluster = None
session = None

def connect():
    global cluster, session
    while True:
        try:
            cluster = Cluster([host], auth_provider=auth, connect_timeout=30)
            session = cluster.connect()
            session.execute('SELECT now() FROM system.local')
            print('Connected.', flush=True)
            return
        except Exception as e:
            print('Not ready:', e, flush=True)
            try:
                cluster.shutdown()
            except Exception:
                pass
            cluster = None
            time.sleep(5)

connect()
print('Hammering writes...', flush=True)
n = 0
errors = 0
while True:
    try:
        val = ''.join(random.choices(string.ascii_letters, k=32))
        session.execute(
            'INSERT INTO bench_ks.events (id, val) VALUES (%s, %s)',
            (n % 10000, val)
        )
        n += 1
        errors = 0
        if n % 1000 == 0:
            print(f'{n} writes', flush=True)
    except Exception as e:
        errors += 1
        print('Error:', e, flush=True)
        if errors > 20:
            print('Too many errors — reconnecting', flush=True)
            try:
                cluster.shutdown()
            except Exception:
                pass
            connect()
            errors = 0
        time.sleep(0.5)
"""

        configmap = f"""\
apiVersion: v1
kind: ConfigMap
metadata:
  name: cassandra-writer-script
  namespace: {self.namespace}
data:
  writer.py: |
{chr(10).join("    " + line for line in writer_script.splitlines())}
"""

        manifest = f"""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cassandra-writer
  namespace: {self.namespace}
  labels:
    app: cassandra-writer
spec:
  replicas: 1
  selector:
    matchLabels:
      app: cassandra-writer
  template:
    metadata:
      labels:
        app: cassandra-writer
    spec:
      containers:
      - name: writer
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
        args: ["pip install cassandra-driver -q && python3 /writer.py"]
        volumeMounts:
        - name: script
          mountPath: /writer.py
          subPath: writer.py
      volumes:
      - name: script
        configMap:
          name: cassandra-writer-script
"""
        for name, yaml in [("ConfigMap", configmap), ("Deployment", manifest)]:
            result = subprocess.run(
                "kubectl apply -f -",
                shell=True, input=yaml, capture_output=True, text=True,
            )
            if result.returncode != 0:
                logger.warning(f"[CassandraWriteCacheOom] Writer {name} deploy failed: {result.stderr.strip()}")
                return
        logger.info("[CassandraWriteCacheOom] Write-loop workload deployed")

    @mark_fault_injected
    def recover_fault(self):
        subprocess.run(
            f"kubectl delete deployment cassandra-writer -n {self.namespace} --ignore-not-found"
            f" && kubectl delete configmap cassandra-writer-script -n {self.namespace} --ignore-not-found",
            shell=True, check=False,
        )
        logger.info("[CassandraWriteCacheOom] Writer workload deleted")
