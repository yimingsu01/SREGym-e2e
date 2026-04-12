"""Cassandra OOM: unbounded compaction analytics cache in CompactionTask causes heap exhaustion.

Bug: CompactionTask.runMayThrow() retains an 8 MB byte array in a static
     List<byte[]> (compactionAnalyticsCache) after every compaction run that
     touches the bench_ks keyspace.  The list is never drained or bounded, so
     heap usage grows by 8 MB for every compaction event.

     SizeTieredCompactionStrategy fires frequently under steady write workload
     (each memtable flush produces a new SSTable; every 2+ SSTables of similar
     size trigger a merge compaction).  With a 512 MB JVM heap the node exhausts
     its heap after ~60 compaction events and crashes:

         java.lang.OutOfMemoryError: Java heap space

     Kubernetes OOMKills the container and restarts it.  The writer pod
     reconnects, resumes inserts, new SSTables are flushed, more compactions
     fire, and the heap fills again → CrashLoopBackOff.

     The crash is asynchronous: it happens in Cassandra's background compaction
     thread, not in the query-handling thread, so the OutOfMemoryError appears
     in the log alongside compaction INFO messages rather than alongside query
     errors.

Root cause: CompactionTask.java — compactionAnalyticsCache static field
     accumulates one 8 MB frame per compaction of bench_ks and is never cleared.

Fix: remove the compactionAnalyticsCache field and its population block in
     runMayThrow().
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
    ) WITH compaction = {
        'class': 'SizeTieredCompactionStrategy',
        'min_threshold': 2
    };
"""

# Two batches of seed rows — each batch is flushed to its own SSTable so that
# SizeTieredCompactionStrategy (min_threshold=2) fires a compaction immediately.
_SEED_BATCH_1 = "\n".join(
    f"INSERT INTO bench_ks.events (id, val) VALUES ({i}, '{'seed_' + str(i):_<64}');"
    for i in range(2000)
)
_SEED_BATCH_2 = "\n".join(
    f"INSERT INTO bench_ks.events (id, val) VALUES ({i}, '{'updt_' + str(i):_<64}');"
    for i in range(2000, 4000)
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


class CassandraCompactionOom(CassandraCustomBuildProblem):
    """Cassandra crashes with OOM due to unbounded static cache in the background compaction path.

    Fault injection:
      1. Deploys Cassandra 4.1.7 built from a patched source where
         CompactionTask.runMayThrow() accumulates 8 MB per compaction run into a
         static List<byte[]> (compactionAnalyticsCache) that is never cleared.
      2. Creates keyspace bench_ks with SizeTieredCompactionStrategy
         (min_threshold=2 so compaction fires as soon as two SSTables exist).
      3. Seeds two batches of rows and explicitly flushes each to disk, creating
         two SSTables and triggering an immediate first compaction (+8 MB on heap).
      4. Deploys a writer Deployment that inserts rows with monotonically
         increasing IDs, generating a continuous stream of new data.
         Each memtable flush produces a new SSTable; every two SSTables trigger
         another compaction → another 8 MB retained on heap.
         With a 512 MB JVM heap, ~60 compaction events exhaust memory.
         Kubernetes OOMKills the pod, it restarts, and the cycle repeats →
         CrashLoopBackOff.

    The agent observes OOMKilled Cassandra pods, finds OutOfMemoryError in logs
    originating from the compaction thread (not the query path), and must locate
    the unbounded compactionAnalyticsCache in CompactionTask.java and remove it.
    """

    cassandra_version = "4.1.7"
    source_git_ref = "cassandra-4.1.7"
    patch_dir = Path(__file__).parent / "patches" / "cassandra_compaction_oom"

    root_cause_file = "src/java/org/apache/cassandra/db/compaction/CompactionTask.java"
    root_cause_description = (
        "CompactionTask.runMayThrow() (CompactionTask.java) appends an 8 MB byte array "
        "to a static, unbounded List<byte[]> (compactionAnalyticsCache) after every "
        "compaction run that touches the bench_ks keyspace.  The list is never cleared "
        "or size-capped, so heap usage grows by 8 MB per compaction event.  Under a "
        "sustained write workload SizeTieredCompactionStrategy fires frequently "
        "(every two SSTable flushes), so the 512 MB JVM heap is exhausted after roughly "
        "60 compaction events, crashing the node with OutOfMemoryError: Java heap "
        "space.  The error appears in the compaction thread's log context rather than in "
        "a query-handling thread, making it harder to correlate with client-side "
        "failures.  Kubernetes OOMKills the pod and restarts it; the writer resumes "
        "immediately, new compactions fire, and the node enters CrashLoopBackOff.  "
        "Fix: remove the compactionAnalyticsCache field and its accumulation block in "
        "runMayThrow()."
    )

    trigger_cql = _SETUP_CQL

    def _create_app(self) -> Cassandra:
        return _CassandraWithOomKill(cassandra_version=self.cassandra_version)

    def _run_nodetool(self, *args: str) -> None:
        """Run a nodetool command on the first available Cassandra pod."""
        pod = subprocess.run(
            f"kubectl get pods -n {self.namespace} -l app.kubernetes.io/name=cassandra "
            f"-o jsonpath='{{.items[0].metadata.name}}'",
            shell=True, capture_output=True, text=True,
        ).stdout.strip().strip("'")
        if not pod:
            logger.warning("[CassandraCompactionOom] No Cassandra pod found for nodetool")
            return
        result = subprocess.run(
            f"kubectl exec -n {self.namespace} {pod} -c cassandra -- nodetool {' '.join(args)}",
            shell=True, capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.warning(f"[CassandraCompactionOom] nodetool {args} failed: {result.stderr.strip()}")
        else:
            logger.info(f"[CassandraCompactionOom] nodetool {args}: {result.stdout.strip()}")

    @mark_fault_injected
    def inject_fault(self):
        self._apply_buggy_image()

        logger.info("[CassandraCompactionOom] Setting up bench keyspace")
        self.app.run_cql(_SETUP_CQL)

        # Seed first batch of rows then flush to SSTable 1.
        logger.info("[CassandraCompactionOom] Seeding batch 1 and flushing to SSTable")
        self.app.run_cql(_SEED_BATCH_1)
        self._run_nodetool("flush", "bench_ks")

        # Seed second batch of rows then flush to SSTable 2.
        # SizeTieredCompactionStrategy (min_threshold=2) fires immediately,
        # merging SSTable 1 + SSTable 2 → first 8 MB compaction analytics frame.
        logger.info("[CassandraCompactionOom] Seeding batch 2 and flushing — first compaction fires")
        self.app.run_cql(_SEED_BATCH_2)
        self._run_nodetool("flush", "bench_ks")

        logger.info("[CassandraCompactionOom] Deploying writer workload — will drive continuous compactions")
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
print('Writing unique rows to drive compaction...', flush=True)
n = 4000  # start after the seeded rows
errors = 0
while True:
    try:
        val = ''.join(random.choices(string.ascii_letters, k=64))
        session.execute(
            'INSERT INTO bench_ks.events (id, val) VALUES (%s, %s)',
            (n, val)
        )
        n += 1
        errors = 0
        if n % 5000 == 0:
            print(f'{n} rows written', flush=True)
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
  name: cassandra-compaction-writer-script
  namespace: {self.namespace}
data:
  writer.py: |
{chr(10).join("    " + line for line in writer_script.splitlines())}
"""

        manifest = f"""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cassandra-compaction-writer
  namespace: {self.namespace}
  labels:
    app: cassandra-compaction-writer
spec:
  replicas: 1
  selector:
    matchLabels:
      app: cassandra-compaction-writer
  template:
    metadata:
      labels:
        app: cassandra-compaction-writer
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
          name: cassandra-compaction-writer-script
"""
        for name, yaml in [("ConfigMap", configmap), ("Deployment", manifest)]:
            result = subprocess.run(
                "kubectl apply -f -",
                shell=True, input=yaml, capture_output=True, text=True,
            )
            if result.returncode != 0:
                logger.warning(f"[CassandraCompactionOom] Writer {name} deploy failed: {result.stderr.strip()}")
                return
        logger.info("[CassandraCompactionOom] Writer workload deployed")

    @mark_fault_injected
    def recover_fault(self):
        subprocess.run(
            f"kubectl delete deployment cassandra-compaction-writer -n {self.namespace} --ignore-not-found"
            f" && kubectl delete configmap cassandra-compaction-writer-script -n {self.namespace} --ignore-not-found",
            shell=True, check=False,
        )
        logger.info("[CassandraCompactionOom] Writer workload deleted")
