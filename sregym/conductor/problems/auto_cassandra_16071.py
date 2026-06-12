"""CASSANDRA-16071: SASI `max_compaction_flush_memory_in_mb` is interpreted as BYTES
(not MB) during compaction-time index rebuilds, causing a temp-segment explosion that
exhausts vm.max_map_count and crashes the JVM.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16071  (component: Feature/SASI)
Buggy: 3.11.7  ->  Fixed: 3.11.8

Reproduction summary (single node, topology=1node CONFIRMED in the evidence log):
  Create a SASI custom index with OPTIONS = {'max_compaction_flush_memory_in_mb':'1'}.
  On 3.11.7 the '1' is used directly as a 1-BYTE per-segment flush threshold (the
  `1048576L *` MB multiplier is MISSING from IndexMode.java). Load ~100k small rows in
  3 flushed batches (=> 3 SSTables), then `nodetool compact`. The COMPACTION code path
  (PerSSTableIndexWriter.java:363 returns the configured number as a raw byte threshold;
  the FLUSH path uses a hardcoded 1GB, so ONLY compaction triggers the bug) flushes one
  OnDiskIndex segment file per posting (~1 segment per row), producing 64k+ temp files.
  The merge phase mmaps them all and exhausts vm.max_map_count (65530), throwing
  `OutOfMemoryError: Map failed` -> native memory exhaustion -> JVM killed -> pod restart.

VERBATIM BUGGY SIGNATURE (3.11.7, from the reproduction evidence log):
  ERROR [SASI-General:9] PerSSTableIndexWriter.java:262 - Failed to build index segment
    .../md-6-big-SI_t_v_sasi.db_64360
  org.apache.cassandra.io.FSReadError: java.io.IOException: Map failed
      at org.apache.cassandra.io.util.ChannelProxy.map(ChannelProxy.java:157)
      at org.apache.cassandra.index.sasi.utils.MappedBuffer.<init>(MappedBuffer.java:78)
      at org.apache.cassandra.index.sasi.disk.OnDiskIndex.<init>(OnDiskIndex.java:145)
      at org.apache.cassandra.index.sasi.disk.PerSSTableIndexWriter$Index.lambda$scheduleSegmentFlush$0(PerSSTableIndexWriter.java:258)
      ...
  Caused by: java.io.IOException: Map failed
      at sun.nio.ch.FileChannelImpl.map(FileChannelImpl.java:938)
  Caused by: java.lang.OutOfMemoryError: Map failed
      at sun.nio.ch.FileChannelImpl.map0(Native Method)
  Then the JVM dies: "There is insufficient memory for the Java Runtime Environment to
  continue. Native memory allocation (malloc) failed ..." -> pod restartCount=1.

Shape: nodetool-sequence (single node). The fault CANNOT be expressed as a plain CQL
`reproducer` string run through the generic cqlsh loop, because it requires `nodetool
flush` between data-load batches and a final `nodetool compact`, all of which run on the
SERVER pod (not the cqlsh client pod). We therefore override inject_fault() to run the
whole CREATE/LOAD/flush*3/compact sequence via `kubectl exec` on the Cassandra server
pod (the same exec technique used by cassandra_20108.py, ported onto GenericCustomBuildProblem).

continuous_reproducer is intentionally False (diagnosis-only, mitigation_oracle=None):
  * The generic Cassandra reproducer loop is CQL-only (cqlsh from a client pod) and
    cannot run nodetool or reliably reload 100k rows each iteration.
  * This is a ONE-SHOT compaction crash: the evidence log shows restartCount=1 (a single
    restart, not an escalating CrashLoopBackOff) — the node recovers after the restart,
    so a liveness/readiness-based mitigation probe would falsely report the bug "fixed."
"""

import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoCassandra16071(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "3.11.7"
    source_git_ref = "cassandra-3.11.7"
    # The bug ships in the stock 3.11.7 image (fix is 3.11.8), so re-tag the stock
    # image instead of running a ~30-min `ant jar` source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/index/sasi/conf/IndexMode.java"
    root_cause_description = (
        "SASI index option 'max_compaction_flush_memory_in_mb' is interpreted as a raw BYTE "
        "count instead of megabytes during compaction-time index rebuilds. In IndexMode.java "
        "(3.11.7) the option is parsed with Long.parseLong(...) and stored directly, MISSING the "
        "'1048576L *' MB->bytes multiplier (the 3.11.8 fix parses '1048576L * Long.parseLong(...)' "
        "and renames the field to maxCompactionFlushMemoryInBytes). "
        "PerSSTableIndexWriter.java:363 then returns this number as the per-segment byte flush "
        "threshold for OperationType.COMPACTION (the FLUSH path uses a hardcoded 1GB, so only "
        "compaction is affected). With '1' the threshold is 1 byte, so the compaction-path SASI "
        "rebuild flushes one OnDiskIndex segment file per posting (~1 per row); the merge phase "
        "mmaps all 64k+ temp files, exhausting vm.max_map_count and throwing "
        "java.lang.OutOfMemoryError: Map failed, which crashes the JVM and restarts the node."
    )

    # NOTE: this string is the faithful, human-readable record of the buggy reproduction
    # (used by the diagnosis judge / for the record). The fault is actually driven by the
    # inject_fault() override below, because the flush/compact steps must run on the server
    # pod and cannot go through the generic CQL-only reproducer loop.
    reproducer = """
-- 1) Schema + SASI index whose buggy byte-threshold ('1' == 1 BYTE on 3.11.7) forces a
--    new on-disk index segment per posting during the compaction-path rebuild.
CREATE KEYSPACE IF NOT EXISTS repro16071
  WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE IF NOT EXISTS repro16071.t (id int PRIMARY KEY, v text);
CREATE CUSTOM INDEX IF NOT EXISTS t_v_sasi ON repro16071.t (v)
  USING 'org.apache.cassandra.index.sasi.SASIIndex'
  WITH OPTIONS = {'mode':'PREFIX','max_compaction_flush_memory_in_mb':'1'};

-- 2) Load ~100,000 small rows in 3 batches, running `nodetool flush` after EACH batch so
--    the data lands in 3 separate SSTables (the flush-between-batches is essential — it is
--    what gives compaction multiple SSTables to merge). e.g. per batch:
--      COPY repro16071.t (id, v) FROM '/tmp/batch_N.csv';   (each row v = 'val_<id padded>')
--      nodetool flush repro16071 t                          (run on the SERVER pod)
--    Repeat for batches 0, 1, 2  =>  3 SSTables.

-- 3) Compact: OperationType.COMPACTION uses the buggy 1-byte threshold and explodes into
--    64k+ temp segment files -> mmap exhausts vm.max_map_count -> OutOfMemoryError: Map
--    failed -> JVM crash -> pod restart.
--      nodetool compact repro16071 t                        (run on the SERVER pod)
"""

    # One-shot compaction crash, not a continuously re-triggerable CQL query. See the module
    # docstring for the full justification. Diagnosis-only (mitigation_oracle stays None).
    continuous_reproducer = False
    crash_on_startup = False
    # No expected_output: this is a crash/error bug, not a wrong-result bug.

    # ── Reproduction parameters ────────────────────────────────────────────────
    _KEYSPACE = "repro16071"
    _TABLE = "t"
    _SASI_INDEX = "t_v_sasi"
    _TOTAL_ROWS = 100000
    _BATCHES = 3  # => 3 flushed SSTables

    def _server_pod(self) -> str:
        """Return the name of a Running Cassandra server pod in this cluster.

        K8ssandra labels its cassandra StatefulSet pods with
        app.kubernetes.io/instance=<cluster_name>; the cassandra process runs in
        the container named "cassandra" (cass-operator convention). The bug is a
        purely node-local compaction, so any one server pod (the one that owns the
        data after flush) reproduces it.
        """
        out = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-l app.kubernetes.io/instance={self.app.cluster_name} "
            f"--field-selector=status.phase=Running "
            f"-o jsonpath='{{range .items[*]}}{{.metadata.name}} {{end}}'",
            shell=True, capture_output=True, text=True,
        )
        names = [n for n in out.stdout.strip().strip("'").split() if n]
        # Prefer the actual cassandra DC StatefulSet pods (named "<cluster>-dc1-...sts-N")
        # over any operator/sidecar pods that share the instance label.
        sts_pods = [n for n in names if "dc1" in n]
        pod = (sts_pods or names)[0] if (sts_pods or names) else ""
        return pod

    def _exec(self, pod: str, inner_cmd: str, timeout: int = 600):
        """Run a shell command inside the cassandra container of the given pod."""
        return subprocess.run(
            f"kubectl exec -n {self.namespace} {pod} -c cassandra -- "
            f"bash -lc {self._shquote(inner_cmd)}",
            shell=True, capture_output=True, text=True, timeout=timeout,
        )

    @staticmethod
    def _shquote(s: str) -> str:
        return "'" + s.replace("'", "'\\''") + "'"

    @mark_fault_injected
    def inject_fault(self):
        """Drive the SASI segment-explosion compaction crash on the server pod.

        Steps (all on the Cassandra SERVER pod, which has both cqlsh and nodetool):
          1. Ensure the buggy 3.11.7 image is active.
          2. CREATE KEYSPACE / TABLE / CUSTOM INDEX (SASI, '1' => 1 BYTE threshold on 3.11.7).
          3. Load ~100k rows in 3 batches, `nodetool flush` after each => 3 SSTables.
          4. `nodetool compact` => segment explosion => OutOfMemoryError: Map failed => crash.
        """
        # 1) Make sure the buggy image is running (mirror the base-class swap logic so this
        #    override is self-contained).
        if getattr(self, "_predeployed_buggy", False):
            logger.info("[AutoCassandra16071] Buggy image already deployed at cluster start — skipping swap")
        else:
            logger.info(f"[AutoCassandra16071] Swapping cluster to buggy image: {self._custom_image}")
            self.app.inject_buggy_image(self._custom_image)
            logger.info("[AutoCassandra16071] Buggy image active")

        self.setup_preconditions()

        pod = self._server_pod()
        if not pod:
            logger.warning("[AutoCassandra16071] No Running Cassandra server pod found — cannot inject fault")
            return
        logger.info(f"[AutoCassandra16071] Using server pod {pod}")

        # 2) Schema + SASI index. The '1' is taken verbatim from the evidence log: on 3.11.7
        #    it becomes a 1-BYTE per-segment flush threshold for the compaction-path rebuild.
        ddl = (
            f"CREATE KEYSPACE IF NOT EXISTS {self._KEYSPACE} "
            f"WITH replication={{'class':'SimpleStrategy','replication_factor':1}}; "
            f"CREATE TABLE IF NOT EXISTS {self._KEYSPACE}.{self._TABLE} (id int PRIMARY KEY, v text); "
            f"CREATE CUSTOM INDEX IF NOT EXISTS {self._SASI_INDEX} "
            f"ON {self._KEYSPACE}.{self._TABLE} (v) "
            f"USING 'org.apache.cassandra.index.sasi.SASIIndex' "
            f"WITH OPTIONS = {{'mode':'PREFIX','max_compaction_flush_memory_in_mb':'1'}};"
        )
        logger.info("[AutoCassandra16071] Creating keyspace/table/SASI index")
        self._run_cql(pod, ddl)

        # 3) Load ~100k rows in self._BATCHES batches, flushing after each so we end up with
        #    one SSTable per batch. Each row's v is 'val_<zero-padded id>' (small, so the
        #    blow-up is driven by the per-posting segment flush, not by row size).
        per_batch = max(1, self._TOTAL_ROWS // self._BATCHES)
        for b in range(self._BATCHES):
            start = b * per_batch
            end = self._TOTAL_ROWS if b == self._BATCHES - 1 else (b + 1) * per_batch
            logger.info(f"[AutoCassandra16071] Loading batch {b} rows [{start}, {end})")
            # Generate the batch CSV inside the pod and COPY it in, then flush -> one SSTable.
            gen_and_load = (
                f"awk 'BEGIN{{for(i={start};i<{end};i++) printf \"%d,val_%08d\\n\", i, i}}' "
                f"> /tmp/batch_{b}.csv; "
                f"cqlsh -e \"COPY {self._KEYSPACE}.{self._TABLE} (id, v) "
                f"FROM '/tmp/batch_{b}.csv' WITH HEADER=false;\""
            )
            self._exec(pod, gen_and_load)
            logger.info(f"[AutoCassandra16071] nodetool flush after batch {b} (=> 1 SSTable)")
            self._exec(pod, f"nodetool flush {self._KEYSPACE} {self._TABLE}")

        # 4) Compact: the COMPACTION path uses the buggy 1-byte threshold -> ~1 OnDiskIndex
        #    segment file per posting -> 64k+ temp files -> mmap exhausts vm.max_map_count ->
        #    java.lang.OutOfMemoryError: Map failed -> JVM crash -> pod restart.
        logger.info("[AutoCassandra16071] nodetool compact (triggers SASI segment explosion -> OOM Map failed)")
        try:
            self._exec(pod, f"nodetool compact {self._KEYSPACE} {self._TABLE}")
        except Exception as e:
            logger.info(f"[AutoCassandra16071] compact exec ended (expected — node crashes mid-compaction): {e}")

    def _run_cql(self, pod: str, cql: str):
        """Run a CQL string via cqlsh inside the server pod."""
        result = self._exec(pod, f"cqlsh -e {self._shquote(cql)}")
        if result.returncode != 0:
            logger.info(f"[AutoCassandra16071] cqlsh exited {result.returncode}: {result.stderr.strip()[:300]}")
        return result
