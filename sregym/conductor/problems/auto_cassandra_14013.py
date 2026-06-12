"""CASSANDRA-14013: A keyspace literally named "snapshots" loses all row data after a restart.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-14013
Buggy version: 4.1.0  ->  Fixed: 4.0.8 / 4.1.1 / 5.0

Reproduction summary (from the reproduced-bug evidence log):
  Create a keyspace named EXACTLY ``snapshots`` with a table, insert rows, run
  ``nodetool flush snapshots`` (force the rows to on-disk SSTables and discard the
  commitlog), then restart Cassandra IN PLACE (kill PID 1 so the container restarts
  in the same pod and the data directory survives -- NOT ``kubectl delete pod`` when
  using an emptyDir volume). On 4.1.0 the post-restart ``SELECT count(*)`` returns 0
  even though the SSTables are still on disk and the schema in ``system_schema`` is
  intact; the fixed build (4.1.1) returns the full row count.

Root cause (per-node, local startup file enumeration):
  Cassandra's startup SSTable scan is per-node: each node's ``Directories.SSTableLister``
  (src/java/org/apache/cassandra/db/Directories.java) enumerates the table's data
  directories and deliberately excludes the reserved ``snapshots``/``backups``
  subdirectories (``Directories.SNAPSHOT_SUBDIR == "snapshots"``) where real
  snapshots/backups live. A keyspace named ``snapshots`` produces a live data directory
  ``.../data/snapshots/<table>-<id>/`` whose ``snapshots`` path component collides with
  that reserved name, so each node mistakes its own live SSTables for snapshot data and
  skips loading them. The table therefore appears empty (``system_schema`` is unaffected,
  so the table still "exists") even though every node's SSTables remain physically on disk.

Verbatim buggy signature (count after in-place restart == 0):
    $ kubectl exec -n <ns> cass -- cqlsh -e "SELECT count(*) FROM snapshots.test_idx;"

     count
    -------
         0

    (1 rows)

    Warnings :
    Aggregation query used without partition key

  ... while the SSTables remain physically on disk (same files, same timestamp),
  proving a LOAD/skip bug rather than data deletion.

Reproduction shape: nodetool-sequence. The bug is per-node, so it reproduces on the
standard multi-node deploy as long as EVERY node is flushed and restarted IN PLACE
(its data directory must survive the restart). ``inject_fault()`` below runs the full
sequence (CQL setup + ``nodetool flush snapshots`` + in-place ``kill 1`` restart of
EVERY cassandra pod + the ``SELECT count(*)`` signature) via ``kubectl exec``.

Notes on the validated method (from the evidence log):
  * "Restart" == ``kubectl exec ... -- kill 1`` (kill PID 1 -> the container restarts
    inside the SAME pod; the data directory survives). With an emptyDir data volume you
    must NEVER ``kubectl delete pod`` -- that wipes the emptyDir and yields a FALSE
    POSITIVE (count 0) on BOTH the buggy and the fixed build. A PVC-backed data volume
    survives either way; the in-place restart is what matters (a fresh bootstrap would
    not exercise the startup load path the same way).
  * The bug is per-node local file enumeration (not gossip/coordinator logic). On a
    multi-node RF=1 cluster, restarting only ONE node gives PARTIAL loss (that node's
    share). Flushing + in-place-restarting ALL nodes makes every node skip its
    ``snapshots`` SSTables -> the clean count == 0.
  * The keyspace name MUST be exactly ``snapshots`` -- the bug is name-triggered.
"""

import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoCassandra14013(GenericCustomBuildProblem):
    db_name = "cassandra"
    # 4.1.0 already ships the bug (fix landed in 4.1.1), so deploy the STOCK 4.1.0
    # image instead of running a ~30-min `ant jar` source build.
    db_version = "4.1.0"
    source_git_ref = "cassandra-4.1.0"
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/db/Directories.java"
    root_cause_description = (
        "A keyspace literally named \"snapshots\" loses all row data after a Cassandra "
        "process restart. Cassandra's startup SSTable scan is per-node: each node's "
        "Directories.SSTableLister enumerates the table's data directories and excludes "
        "the reserved snapshot/backup subdirectories (Directories.SNAPSHOT_SUBDIR == "
        "\"snapshots\"), where real snapshots/backups live. A keyspace named \"snapshots\" "
        "produces a live data directory (.../data/snapshots/<table>-<id>/) whose "
        "\"snapshots\" path component collides with that reserved name, so each node "
        "mistakes its own live SSTables for snapshot data and skips loading them. The "
        "table then appears empty (system_schema is unaffected, so the table still "
        "'exists') even though every node's SSTables remain physically on disk."
    )

    # Full reproduction (derived from the evidence log). The CQL portion creates the
    # name-triggering keyspace + table and 20 rows; the flush + in-place restart + the
    # post-restart SELECT are out-of-band steps run by inject_fault() (a separate client
    # pod cannot flush/restart the server). The keyspace name MUST be exactly `snapshots`.
    reproducer = """
-- STEP 1-3: schema + data (keyspace name MUST be exactly "snapshots")
CREATE KEYSPACE IF NOT EXISTS snapshots WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};
CREATE TABLE IF NOT EXISTS snapshots.test_idx (key text, seqno bigint, primary key(key));
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key1', 1);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key2', 2);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key3', 3);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key4', 4);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key5', 5);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key6', 6);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key7', 7);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key8', 8);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key9', 9);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key10', 10);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key11', 11);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key12', 12);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key13', 13);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key14', 14);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key15', 15);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key16', 16);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key17', 17);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key18', 18);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key19', 19);
INSERT INTO snapshots.test_idx (key, seqno) VALUES ('key20', 20);
-- STEP 4: pre-restart count -> 20
SELECT count(*) FROM snapshots.test_idx;
-- STEP 5 (out-of-band, NOT CQL): nodetool flush snapshots on EVERY node (rows -> on-disk SSTables, commitlog discarded)
-- STEP 6 (out-of-band, NOT CQL): in-place restart EVERY node via `kill 1` (data dir survives; NEVER delete pod w/ emptyDir)
-- STEP 7 (out-of-band): wait for all pods to be Ready again
-- STEP 8-9: post-restart -> schema survives, but count == 0 on the buggy 4.1.0 build (== 20 on fixed 4.1.1)
SELECT count(*) FROM snapshots.test_idx;
"""
    # The continuous-reproducer wiring gives this problem the diagnosis LLM-as-a-judge
    # oracle (on root_cause) AND a ReproducerPodMitigationOracle. NOTE: the mitigation
    # probe runs the reproducer CQL from a SEPARATE client pod, so it cannot flush +
    # in-place-restart the server -- and the reproducer re-INSERTs the 20 rows each
    # iteration -- so the probe always reads 20 and is effectively INERT for this
    # restart-gated bug (it cannot observe the load-skip). The diagnosis oracle is the
    # meaningful one here. expected_output is intentionally left unset (a wrong-result
    # probe greps for the buggy value "0" that the probe pod can never produce, which
    # would only flip the oracle to the opposite wrong verdict).
    continuous_reproducer = True

    # ── Fault injection: flush + in-place restart EVERY node, then read the count ──────
    _COUNT_CQL = "SELECT count(*) FROM snapshots.test_idx;"

    def _cassandra_pods(self) -> list[str]:
        """Return all Cassandra server pods in the cluster namespace."""
        out = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-l app.kubernetes.io/instance={self.app.cluster_name} "
            f"--no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null",
            shell=True, capture_output=True, text=True,
        ).stdout
        return [p.strip() for p in out.splitlines() if p.strip()]

    @mark_fault_injected
    def inject_fault(self):
        """Run the full CASSANDRA-14013 sequence.

        Mirrors the cassandra_20108 kubectl-exec pattern but adds the per-node flush +
        in-place ``kill 1`` restart the bug requires. Because the startup load-skip is
        per-node, EVERY cassandra pod must be flushed and restarted in place (restarting
        only one node on an RF=1 ring gives partial loss, not the clean count == 0). The
        in-place restart preserves each node's data directory (PVC survives; an emptyDir
        would too -- the rule is restart-in-place, never delete the pod).
        """
        # Ensure the buggy image is active and run the standard reproducer-pod wiring
        # (CQL setup + continuous reproducer) via the base-class lifecycle.
        super().inject_fault()

        pods = self._cassandra_pods()
        if not pods:
            logger.warning("[AutoCassandra14013] No Cassandra pods found — skipping flush+restart steps")
            return

        # Flush the `snapshots` keyspace on every node so its rows are on-disk SSTables
        # (and the commitlog is discarded -> replay cannot mask the load-skip).
        for pod in pods:
            logger.info(f"[AutoCassandra14013] pod={pod}: nodetool flush snapshots")
            subprocess.run(
                f"kubectl exec -n {self.namespace} {pod} -c cassandra -- nodetool flush snapshots",
                shell=True, capture_output=True, text=True,
            )

        # In-place restart of EVERY node: kill PID 1 so each container restarts inside the
        # SAME pod and its data directory survives. NEVER `kubectl delete pod` (with an
        # emptyDir data volume that wipes the data -> false positive on both builds).
        for pod in pods:
            logger.info(f"[AutoCassandra14013] pod={pod}: in-place restart via kill 1 (NOT pod delete)")
            subprocess.run(
                f"kubectl exec -n {self.namespace} {pod} -c cassandra -- kill 1",
                shell=True, capture_output=True, text=True,
            )

        # Wait for every in-place-restarted container to become Ready again.
        for pod in pods:
            subprocess.run(
                f"kubectl wait pod/{pod} -n {self.namespace} "
                f"--for=condition=Ready --timeout=300s",
                shell=True, capture_output=True, text=True,
            )

        # Post-restart signature: count == 0 on the buggy 4.1.0 build (== 20 on fixed 4.1.1).
        res = subprocess.run(
            f"kubectl exec -n {self.namespace} {pods[0]} -c cassandra -- "
            f"cqlsh -e \"{self._COUNT_CQL}\"",
            shell=True, capture_output=True, text=True,
        )
        logger.info(
            f"[AutoCassandra14013] post-restart SELECT count(*) -> {res.stdout.strip()[:200]} "
            f"(buggy 4.1.0 expects 0)"
        )
