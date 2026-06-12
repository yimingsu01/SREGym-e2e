"""CASSANDRA-14204: AssertionError in `nodetool garbagecollect` when a table has
`only_purge_repaired_tombstones=true` and a MIX of repaired + unrepaired sstables.

Title: Remove unrepaired SSTables from garbage collection when
       `only_purge_repaired_tombstones` is true (avoids AssertionError).
JIRA:  https://issues.apache.org/jira/browse/CASSANDRA-14204

Buggy: 4.1.1  ->  Fixed: 4.1.3 (also 3.11.16, 4.0.11, 5.0-alpha1, 5.0).
  (The bug was reproduced on stock cassandra:4.1.1 — a pre-fix release on the
   4.1 line with the IDENTICAL unfixed code path. The original candidate buggy
   tag 4.1.2 could not be pulled from Docker Hub, see the evidence log; 4.1.1 is
   < the 4.1.3 fix so it carries the bug. A/B-controlled against fixed 4.1.11.)

Reproduction summary (single node, local compaction):
  Create a table WITH compaction={'class':'SizeTieredCompactionStrategy',
  'only_purge_repaired_tombstones':'true'}. Build a MIX of one REPAIRED sstable
  and one UNREPAIRED sstable, then run `nodetool garbagecollect <ks> <table>`.
  With the flag on, filterSSTables() drops the unrepaired sstable from the
  returned set but it remains in the compaction transaction -> the size assertion
  in parallelAllSSTableOperation fails. (All-unrepaired does NOT fire it: it
  short-circuits with "No sstables to GARBAGE_COLLECT".)

Verbatim buggy signature (cassandra:4.1.1, `nodetool garbagecollect`, exit 2):

    error: null
    -- StackTrace --
    java.lang.AssertionError
        at org.apache.cassandra.db.compaction.CompactionManager.parallelAllSSTableOperation(CompactionManager.java:407)
        at org.apache.cassandra.db.compaction.CompactionManager.performGarbageCollection(CompactionManager.java:620)
        at org.apache.cassandra.db.ColumnFamilyStore.garbageCollect(ColumnFamilyStore.java:1720)
        at org.apache.cassandra.service.StorageService.garbageCollect(StorageService.java:3958)
        ...
    command terminated with exit code 2

Shape: nodetool/flush sequence (NOT pure CQL, NOT wrong-result). The repaired
sstable is minted via `nodetool repair` (incremental anticompaction on the
RF=3 K8ssandra cluster marks the batch-1 sstable repaired with no daemon
stop/offline tool). inject_fault() is overridden to run the full nodetool+CQL
sequence on a single Cassandra pod via `kubectl exec`. See inject_fault() for
the log-verified offline `sstablerepairedset` fallback.
"""

import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

# Keyspace / table used by the reproducer.
_KS = "repro14204"
_TBL = "t"

# Step 1 — table with the bug-gating flag. RF=3 so the data lands on every pod
# regardless of token hashing AND so incremental `nodetool repair` actually runs
# (RF=1 single-node repair short-circuits with "No repair is needed", which is
# why the evidence log fell back to the offline sstablerepairedset tool).
_CREATE_CQL = (
    f"CREATE KEYSPACE IF NOT EXISTS {_KS} "
    f"WITH replication = {{'class':'SimpleStrategy','replication_factor':3}}; "
    f"CREATE TABLE IF NOT EXISTS {_KS}.{_TBL} (id int PRIMARY KEY, v text) "
    f"WITH compaction = {{'class':'SizeTieredCompactionStrategy',"
    f"'only_purge_repaired_tombstones':'true'}};"
)

# Step 2 — batch 1 (will be marked REPAIRED). Includes a DELETE so there is a
# tombstone for garbagecollect to act on.
_BATCH1_CQL = (
    f"INSERT INTO {_KS}.{_TBL}(id,v) VALUES (1,'a'); "
    f"INSERT INTO {_KS}.{_TBL}(id,v) VALUES (2,'b'); "
    f"DELETE FROM {_KS}.{_TBL} WHERE id=2;"
)

# Step 4 — batch 2 (stays UNREPAIRED) -> now a MIX of 1 repaired + 1 unrepaired.
_BATCH2_CQL = (
    f"INSERT INTO {_KS}.{_TBL}(id,v) VALUES (3,'c'); "
    f"INSERT INTO {_KS}.{_TBL}(id,v) VALUES (4,'d'); "
    f"DELETE FROM {_KS}.{_TBL} WHERE id=4;"
)


class AutoCassandra14204(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.1.1"
    source_git_ref = "cassandra-4.1.1"
    # 4.1.1 already ships the bug, so deploy the stock image instead of an
    # ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/db/compaction/CompactionManager.java"
    root_cause_description = (
        "`nodetool garbagecollect` throws java.lang.AssertionError in "
        "CompactionManager.parallelAllSSTableOperation when the table has "
        "compaction option only_purge_repaired_tombstones=true and holds a mix of "
        "repaired and unrepaired sstables. CompactionManager$6.filterSSTables() "
        "removes the unrepaired sstables from the returned candidate set but they "
        "are NOT removed from the compaction transaction, so the size assertion "
        "(filtered.size() == transaction.originals().size()) in "
        "parallelAllSSTableOperation fails. The fix removes the unrepaired sstables "
        "from the GC transaction so the returned set matches the transaction."
    )

    # Authoritative buggy steps from the evidence log. inject_fault() executes
    # this sequence operationally (the bug needs nodetool flush/repair +
    # garbagecollect, which a single CQL string cannot express); this string is
    # the human-readable record of what is run and is also surfaced to oracles.
    reproducer = """
-- CASSANDRA-14204 reproducer (nodetool/flush sequence, run on ONE Cassandra pod)

-- 1. Table with the bug-gating flag (RF=3 so the partition exists on every pod
--    and so incremental `nodetool repair` actually anticompacts).
CREATE KEYSPACE IF NOT EXISTS repro14204
    WITH replication = {'class':'SimpleStrategy','replication_factor':3};
CREATE TABLE IF NOT EXISTS repro14204.t (id int PRIMARY KEY, v text)
    WITH compaction = {'class':'SizeTieredCompactionStrategy',
                       'only_purge_repaired_tombstones':'true'};

-- 2. Batch 1 (becomes the REPAIRED sstable) + flush.
INSERT INTO repro14204.t(id,v) VALUES (1,'a');
INSERT INTO repro14204.t(id,v) VALUES (2,'b');
DELETE FROM repro14204.t WHERE id=2;
-- nodetool flush repro14204 t

-- 3. Mark batch-1 sstable REPAIRED.
--    nodetool repair repro14204 t            (incremental; anticompacts -> repaired)
--    GATE: nodetool tablestats repro14204.t  -> Percent repaired > 0

-- 4. Batch 2 (stays UNREPAIRED) + flush  -> MIX: 1 repaired + 1 unrepaired.
INSERT INTO repro14204.t(id,v) VALUES (3,'c');
INSERT INTO repro14204.t(id,v) VALUES (4,'d');
DELETE FROM repro14204.t WHERE id=4;
-- nodetool flush repro14204 t
-- GATE: nodetool tablestats repro14204.t -> SSTable count: 2, 0 < Percent repaired < 100

-- 5. THE REPRODUCER -> java.lang.AssertionError in parallelAllSSTableOperation, exit 2.
-- nodetool garbagecollect repro14204 t
"""

    # Error/crash bug (AssertionError), NOT wrong-result -> leave expected_output
    # unset so the diagnosis oracle judges the root cause and no buggy-value grep
    # is installed.
    #
    # continuous_reproducer is False: this bug is a one-shot, stateful nodetool
    # sequence (build a specific repaired+unrepaired sstable mix, then fire one
    # garbagecollect). The shared continuous-reproducer probe loops a pure-CQL
    # `cqlsh < run.cql`, which cannot express the nodetool/flush/repair steps, so
    # enabling it would deploy a probe pod + mitigation oracle that never
    # reproduce this bug. Diagnosis-only, mirroring cassandra_20108.
    continuous_reproducer = False

    # ── Custom fault injection (nodetool/flush sequence) ───────────────────────

    def _server_pod(self) -> str | None:
        """Name of one Cassandra server pod in the deployed cluster."""
        out = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-l app.kubernetes.io/instance={self.app.cluster_name} "
            f"-o jsonpath='{{.items[0].metadata.name}}'",
            shell=True, capture_output=True, text=True,
        ).stdout.strip().strip("'")
        return out or None

    def _exec(self, pod: str, inner: str, *, timeout: int = 180) -> subprocess.CompletedProcess:
        """Run a shell command inside the `cassandra` container of `pod`."""
        cmd = (
            f"kubectl exec -n {self.namespace} {pod} -c cassandra -- "
            f"bash -c {subprocess.list2cmdline([inner])}"
        )
        cp = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        logger.info(
            "[AutoCassandra14204] exec rc=%s :: %s\n  stdout=%s\n  stderr=%s",
            cp.returncode, inner[:120], cp.stdout.strip()[:300], cp.stderr.strip()[:300],
        )
        return cp

    def _cqlsh(self, pod: str, cql: str, *, timeout: int = 180) -> subprocess.CompletedProcess:
        """Run a CQL string via cqlsh on the local pod (127.0.0.1)."""
        cp = subprocess.run(
            f"kubectl exec -i -n {self.namespace} {pod} -c cassandra -- cqlsh 127.0.0.1",
            shell=True, input=cql, capture_output=True, text=True, timeout=timeout,
        )
        logger.info(
            "[AutoCassandra14204] cqlsh rc=%s :: %s\n  stdout=%s\n  stderr=%s",
            cp.returncode, cql.replace("\n", " ")[:120],
            cp.stdout.strip()[:300], cp.stderr.strip()[:300],
        )
        return cp

    @mark_fault_injected
    def inject_fault(self):
        """Swap in the buggy image, then drive the CASSANDRA-14204 nodetool
        sequence on a single Cassandra pod so `nodetool garbagecollect` throws
        the AssertionError (visible to the operator and in the system log).

        Sequence (authoritative log steps, mechanism adapted to the K8ssandra
        deploy — see module docstring):
          1. cqlsh: CREATE KEYSPACE (RF=3) + table WITH only_purge_repaired_tombstones=true
          2. cqlsh: INSERT batch 1 (+ DELETE)         ;  nodetool flush
          3. nodetool repair  -> batch-1 sstable becomes REPAIRED (anticompaction)
          4. cqlsh: INSERT batch 2 (+ DELETE)         ;  nodetool flush   (MIX now)
          5. nodetool garbagecollect  -> java.lang.AssertionError, exit 2  (THE BUG)

        Fallback if `nodetool repair` does not mark the sstable repaired in this
        environment: stop Cassandra (mgmt-api lifecycle / `nodetool stopdaemon`),
        run `sstablerepairedset --really-set --is-repaired
        /var/lib/cassandra/data/repro14204/t-*/*-Data.db`, then restart. The
        K8ssandra pod has a PVC, so the repairedAt flag survives a container
        restart (the evidence log only needed the tail-as-PID1 hack because the
        ad-hoc stock pod had NO PVC).
        """
        # Swap the running cluster to the buggy image first (4.1.1 is the buggy
        # build; if it was pre-deployed the base class no-ops the swap).
        if getattr(self, "_predeployed_buggy", False):
            logger.info("[AutoCassandra14204] Buggy image already deployed — skipping swap")
        else:
            logger.info("[AutoCassandra14204] Swapping cluster to buggy image %s", self._custom_image)
            self.app.inject_buggy_image(self._custom_image)

        self.setup_preconditions()

        pod = self._server_pod()
        if not pod:
            logger.warning("[AutoCassandra14204] No Cassandra server pod found — cannot run reproducer")
            return
        logger.info("[AutoCassandra14204] Driving reproducer on pod %s", pod)

        # 1. schema with the bug-gating compaction flag.
        self._cqlsh(pod, _CREATE_CQL)

        # 2. batch 1 + flush.
        self._cqlsh(pod, _BATCH1_CQL)
        self._exec(pod, f"nodetool flush {_KS} {_TBL}")

        # 3. mark batch-1 sstable repaired via incremental repair (anticompaction).
        self._exec(pod, f"nodetool repair {_KS} {_TBL}", timeout=300)
        self._exec(pod, f"nodetool tablestats {_KS}.{_TBL} | grep -i 'percent repaired' || true")

        # 4. batch 2 (unrepaired) + flush -> MIX of repaired + unrepaired.
        self._cqlsh(pod, _BATCH2_CQL)
        self._exec(pod, f"nodetool flush {_KS} {_TBL}")
        self._exec(pod, f"nodetool tablestats {_KS}.{_TBL} | grep -iE 'sstable count|percent repaired' || true")

        # 5. THE REPRODUCER — expected to fail with AssertionError (exit 2).
        gc = self._exec(pod, f"nodetool garbagecollect {_KS} {_TBL}")
        if gc.returncode != 0 and "AssertionError" in (gc.stdout + gc.stderr):
            logger.info("[AutoCassandra14204] Reproduced CASSANDRA-14204: AssertionError in garbagecollect")
        else:
            logger.warning(
                "[AutoCassandra14204] garbagecollect rc=%s did not show the expected "
                "AssertionError (check repaired-sstable mix / consider the offline "
                "sstablerepairedset fallback)", gc.returncode,
            )

    @mark_fault_injected
    def recover_fault(self):
        """Restore the stock image and wait for the cluster to be Ready."""
        logger.info("[AutoCassandra14204] Recovering: restoring cluster to stock image")
        self.app.restore_stock_image(custom_image=self._custom_image)
        logger.info("[AutoCassandra14204] Recovery complete")
