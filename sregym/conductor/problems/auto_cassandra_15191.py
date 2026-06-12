"""CASSANDRA-15191: disk_failure_policy=stop_paranoid is IGNORED on a
CorruptSSTableException thrown AFTER the node is up (e.g. a regular SELECT).

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-15191
Buggy version: 3.11.7  ->  Fixed: 3.0.22 / 3.11.8 / 4.0-beta2 / 4.0

Reproduction summary (from the reproduced-bug evidence log):
  Start a single Cassandra node with cassandra.yaml edited to
  ``disk_failure_policy: stop_paranoid`` (and ``disk_access_mode: standard`` so the
  LZ4 per-chunk CRC is re-validated from disk instead of being served from the mmap
  page cache). Create an LZ4-compressed table, INSERT ~2000 rows, ``nodetool flush``
  them to an on-disk SSTable, then corrupt the body of the ``*-Data.db`` file out of
  band (``dd if=/dev/urandom ... conv=notrunc``). A full-scan ``SELECT *`` then hits
  the corrupt chunk and raises a CorruptSSTableException at read time.

  On the buggy 3.11.7 build the stop_paranoid policy is IGNORED: gossip and the native
  (binary) transport stay RUNNING and the node keeps serving (a fresh ``SELECT now()``
  still succeeds) -- it merely logs the exception. On the fixed 3.11.8 build the policy
  fires: gossip and binary go NOT RUNNING (the JVM stays up for JMX investigation).

Root cause (per the JIRA body, confirmed by the evidence log):
  The exception that reaches the disk-failure-policy check is a ``RuntimeException``
  whose *cause* is the ``CorruptSSTableException`` (it is wrapped while propagating up
  through ``AbstractLocalAwareExecutorService``), so the policy check does not recognise
  it as a corrupt-sstable failure and stop_paranoid is never applied.

Verbatim buggy signature (from the evidence log):
  The trigger SELECT fails identically on BOTH builds (this is NOT the signature):
    <stdin>:1:ReadFailure: Error from server: code=1300 [Replica(s) failed to execute
    read] message="Operation failed - received 0 responses and 1 failures"
    info={'failures': 1, 'received_responses': 0, 'required_responses': 1,
    'consistency': 'ONE'}

  Server log -- ROOT-CAUSE SIGNATURE (RuntimeException wrapping CorruptSSTableException
  as its cause; frame is AbstractLocalAwareExecutorService):
    java.lang.RuntimeException: org.apache.cassandra.io.sstable.CorruptSSTableException:
        Corrupted: /var/lib/cassandra/data/repro15191/t-.../md-1-big-Data.db
      at org.apache.cassandra.service.StorageProxy$DroppableRunnable.run(StorageProxy.java:2656)
      at org.apache.cassandra.concurrent.AbstractLocalAwareExecutorService$FutureTask.run(AbstractLocalAwareExecutorService.java:165)
      at org.apache.cassandra.concurrent.AbstractLocalAwareExecutorService$LocalSessionFutureTask.run(AbstractLocalAwareExecutorService.java:137)
      ...
    Caused by: org.apache.cassandra.io.sstable.CorruptSSTableException: Corrupted: .../md-1-big-Data.db
      at org.apache.cassandra.io.sstable.format.big.BigTableScanner$KeyScanningIterator.computeNext(BigTableScanner.java:405)

  KEY EVIDENCE -- policy IGNORED on 3.11.7 (node alive AFTER the corrupt read):
    statusgossip: running ; statusbinary: running ; SELECT now() succeeds.
    Zero "Stopping gossiper" / "Stopping native transport" / DiskFailure-killer log lines.

Reproduction shape: config-gated + nodetool-sequence (single node). The bug needs a
cassandra.yaml gate (stop_paranoid + standard disk_access_mode), an out-of-band SSTable
corruption, and a flush -- none of which a pure CQL ``reproducer`` string can express, so
``setup_preconditions()`` and ``inject_fault()`` are overridden below (cassandra_20108 /
auto_cassandra_14013 kubectl-exec pattern). It is single-node, NOT a multi-node stub.

NOTE on oracles (why continuous_reproducer is False):
  The discriminating signal here is node LIVENESS (gossip/binary state), which the
  standard ReproducerPodMitigationOracle cannot express -- its probe runs CQL from a
  separate client pod and checks exit code / greps output, but the trigger SELECT raises
  ReadFailure identically on the buggy AND the fixed build. A continuous reproducer would
  therefore report "bug present" on both versions (silently broken on the mitigation
  side). So this is diagnosis-only: continuous_reproducer = False attaches just the
  LLM-as-a-judge diagnosis oracle on root_cause, and expected_output is left None (this is
  an ignored-policy / liveness bug, NOT a wrong-result bug).
"""

import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoCassandra15191(GenericCustomBuildProblem):
    db_name = "cassandra"
    # 3.11.7 already ships the bug (fix landed in 3.11.8), so deploy the STOCK 3.11.7
    # image instead of running a ~30-min `ant jar` source build.
    db_version = "3.11.7"
    source_git_ref = "cassandra-3.11.7"
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/concurrent/AbstractLocalAwareExecutorService.java"
    root_cause_description = (
        "When disk_failure_policy=stop_paranoid and a CorruptSSTableException is thrown "
        "AFTER the server is up (e.g. on a regular SELECT that hits a corrupt SSTable "
        "chunk), the policy is IGNORED: the node should stop gossip + the native "
        "transport but instead just logs the exception and keeps serving. The exception "
        "that reaches the disk-failure-policy check is a RuntimeException whose *cause* is "
        "the CorruptSSTableException (wrapped while propagating up through "
        "AbstractLocalAwareExecutorService), so the policy check does not recognise it as "
        "a corrupt-sstable failure and stop_paranoid is never applied."
    )

    # Full reproduction (derived from the evidence log). The CQL portion creates the
    # LZ4-compressed table and INSERTs 2000 rows; the cassandra.yaml gate, the
    # `nodetool flush`, the out-of-band `dd` corruption of the *-Data.db file, and the
    # liveness probes are out-of-band steps that a CQL-only `reproducer` string cannot
    # express -- they are run by setup_preconditions()/inject_fault() below.
    # Default LZ4 compression is kept on purpose: the per-chunk CRC is what raises
    # CorruptSSTableException once the chunk body is corrupted.
    KEYSPACE = "repro15191"
    TABLE = "t"
    reproducer = """
-- STEP 1 (out-of-band, NOT CQL): set the gate in cassandra.yaml and restart the node so
--   it takes effect (disk_failure_policy is read at startup):
--     disk_failure_policy: stop_paranoid
--     disk_access_mode: standard   (buffered reads re-validate the LZ4 per-chunk CRC
--                                   from disk; with the default mmap mode the corrupted
--                                   bytes are served from page cache and no exception
--                                   fires) -- run by setup_preconditions().
-- STEP 2-3: LZ4-compressed schema + 2000 rows (~400-byte payload each).
CREATE KEYSPACE IF NOT EXISTS repro15191 WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};
CREATE TABLE IF NOT EXISTS repro15191.t (id int PRIMARY KEY, payload text);
-- (inject_fault() inserts 2000 rows via a server-side loop; one representative row shown)
INSERT INTO repro15191.t (id, payload) VALUES (1, 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx');
-- STEP 4 (out-of-band, NOT CQL): nodetool flush repro15191   (rows -> on-disk SSTable).
-- STEP 5 (out-of-band, NOT CQL): corrupt the body of the *-Data.db file:
--     dd if=/dev/urandom of=<Data.db> bs=1 count=8000 seek=200 conv=notrunc
-- STEP 6: TRIGGER -- a full scan hits the corrupt chunk and raises CorruptSSTableException.
SELECT * FROM repro15191.t;
-- STEP 7 (out-of-band): observe node LIVENESS -- on buggy 3.11.7 gossip + binary stay
--   RUNNING and `SELECT now()` still succeeds (policy IGNORED); on fixed 3.11.8 gossip +
--   binary go NOT RUNNING.
"""
    # Diagnosis-only: the liveness signal cannot be probed by the CQL-grep mitigation
    # oracle (see the module docstring), and the trigger SELECT errors identically on both
    # builds, so we do NOT attach a (silently-broken) mitigation oracle.
    continuous_reproducer = False
    # NOT a wrong-result bug -> no expected_output (the SELECT raises ReadFailure on both
    # the buggy and the fixed build; the signature is node liveness, not a returned value).
    expected_output = None

    # ── Helpers ────────────────────────────────────────────────────────────────────────
    _DATA_DIR = "/var/lib/cassandra/data"

    def _cassandra_pods(self) -> list[str]:
        """Return all Cassandra server pods in the cluster namespace (K8ssandra label)."""
        out = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-l app.kubernetes.io/instance={self.app.cluster_name} "
            f"--no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null",
            shell=True, capture_output=True, text=True,
        ).stdout
        return [p.strip() for p in out.splitlines() if p.strip()]

    def _exec(self, pod: str, inner_cmd: str) -> subprocess.CompletedProcess:
        """Run a shell command inside the cassandra container of `pod`."""
        return subprocess.run(
            f"kubectl exec -n {self.namespace} {pod} -c cassandra -- bash -lc {inner_cmd!r}",
            shell=True, capture_output=True, text=True,
        )

    # ── Config gate: set stop_paranoid + standard disk_access_mode, then restart ────────
    def setup_preconditions(self):
        """Edit cassandra.yaml to enable the stop_paranoid gate and force buffered reads,
        then restart the node in place so the (startup-read) disk_failure_policy takes
        effect. Runs during inject_fault() after the buggy image is active and BEFORE the
        reproducer CQL. Without the restart the cassandra.yaml edit is inert; without
        disk_access_mode=standard the corrupted bytes are served from the mmap page cache
        and no CorruptSSTableException ever fires (an environment caveat, not the bug).
        """
        pods = self._cassandra_pods()
        if not pods:
            logger.warning("[AutoCassandra15191] No Cassandra pods found — skipping config gate")
            return
        # The K8ssandra/management-api image stages cassandra.yaml under /config; fall back
        # to the conf dir if that path is absent. Best-effort across image layouts.
        sed_cmd = (
            "set -e; "
            "YAML=/config/cassandra.yaml; "
            "[ -f \"$YAML\" ] || YAML=$(find / -name cassandra.yaml -path '*conf*' 2>/dev/null | head -1); "
            "sed -i 's/^disk_failure_policy:.*/disk_failure_policy: stop_paranoid/' \"$YAML\"; "
            "grep -q '^disk_access_mode:' \"$YAML\" "
            "&& sed -i 's/^disk_access_mode:.*/disk_access_mode: standard/' \"$YAML\" "
            "|| printf '\\ndisk_access_mode: standard\\n' >> \"$YAML\""
        )
        for pod in pods:
            logger.info(f"[AutoCassandra15191] pod={pod}: set disk_failure_policy=stop_paranoid + disk_access_mode=standard")
            self._exec(pod, sed_cmd)
            # Restart in place (kill PID 1) so the startup-read policy is applied; the data
            # directory survives the restart.
            logger.info(f"[AutoCassandra15191] pod={pod}: in-place restart so the gate takes effect")
            subprocess.run(
                f"kubectl exec -n {self.namespace} {pod} -c cassandra -- kill 1",
                shell=True, capture_output=True, text=True,
            )
        for pod in pods:
            subprocess.run(
                f"kubectl wait pod/{pod} -n {self.namespace} "
                f"--for=condition=Ready --timeout=300s",
                shell=True, capture_output=True, text=True,
            )

    # ── Fault injection: insert -> flush -> corrupt SSTable -> trigger -> probe liveness ─
    @mark_fault_injected
    def inject_fault(self):
        """Run the full CASSANDRA-15191 sequence.

        Mirrors the cassandra_20108 / auto_cassandra_14013 kubectl-exec pattern. The base
        ``super().inject_fault()`` swaps in the buggy image, runs ``setup_preconditions()``
        (the cassandra.yaml gate + restart above), and runs the ``reproducer`` CQL
        (CREATE/INSERT). This method then performs the out-of-band steps the bug needs:
        bulk-insert 2000 rows, ``nodetool flush``, corrupt the on-disk ``*-Data.db`` body
        with ``dd``, fire the trigger ``SELECT *``, and probe node liveness (the
        discriminating signal: gossip/binary stay RUNNING on the buggy 3.11.7 build).
        """
        # Image swap + setup_preconditions() (config gate) + reproducer CQL (schema).
        super().inject_fault()

        pods = self._cassandra_pods()
        if not pods:
            logger.warning("[AutoCassandra15191] No Cassandra pods found — skipping flush/corrupt steps")
            return
        pod = pods[0]  # single-node bug; operate on the first (and only) data node

        # Bulk-load ~2000 rows with ~400-byte payloads so the flushed SSTable has enough
        # body to corrupt at a deterministic offset.
        bulk_insert = (
            "PAY=$(head -c 400 < /dev/zero | tr '\\0' 'x'); "
            "{ echo \"USE %(ks)s;\"; "
            "for i in $(seq 1 2000); do "
            "echo \"INSERT INTO %(tbl)s (id, payload) VALUES ($i, '$PAY');\"; "
            "done; } | cqlsh"
        ) % {"ks": self.KEYSPACE, "tbl": self.TABLE}
        logger.info(f"[AutoCassandra15191] pod={pod}: bulk-insert 2000 rows")
        self._exec(pod, bulk_insert)

        # Flush so the rows land in an on-disk SSTable (commitlog no longer masks the read).
        logger.info(f"[AutoCassandra15191] pod={pod}: nodetool flush {self.KEYSPACE}")
        subprocess.run(
            f"kubectl exec -n {self.namespace} {pod} -c cassandra -- nodetool flush {self.KEYSPACE}",
            shell=True, capture_output=True, text=True,
        )

        # Corrupt the body of the keyspace's *-Data.db: overwrite 8000 bytes at offset 200
        # in place (conv=notrunc keeps the file size; bs=1 keeps the offset exact). The LZ4
        # per-chunk CRC then fails on the full scan -> CorruptSSTableException.
        corrupt_cmd = (
            f"set -e; "
            f"D=$(find {self._DATA_DIR}/{self.KEYSPACE} -name '*-Data.db' | head -1); "
            f"dd if=/dev/urandom of=\"$D\" bs=1 count=8000 seek=200 conv=notrunc"
        )
        logger.info(f"[AutoCassandra15191] pod={pod}: corrupt the on-disk *-Data.db body via dd")
        self._exec(pod, corrupt_cmd)

        # TRIGGER: a full scan hits the corrupt chunk. Fails with ReadFailure on BOTH builds
        # (this is NOT the signature); the signature is what happens to node liveness next.
        logger.info(f"[AutoCassandra15191] pod={pod}: TRIGGER full-scan SELECT (expect a corrupt-read failure)")
        self._exec(pod, f"cqlsh -e 'SELECT * FROM {self.KEYSPACE}.{self.TABLE};' 2>&1 || true")

        # Discriminating signal: on the buggy 3.11.7 build gossip + binary stay RUNNING and
        # the node keeps serving (policy IGNORED); the fixed 3.11.8 build flips them to
        # NOT RUNNING. Probe and log it.
        gossip = self._exec(pod, "nodetool statusgossip 2>&1 || true").stdout.strip()
        binary = self._exec(pod, "nodetool statusbinary 2>&1 || true").stdout.strip()
        alive = self._exec(pod, "cqlsh -e 'SELECT now() FROM system.local;' 2>&1 || true").stdout.strip()
        logger.info(
            f"[AutoCassandra15191] post-trigger liveness (buggy 3.11.7 keeps serving): "
            f"statusgossip={gossip!r} statusbinary={binary!r} select_now_ok={('now' in alive)!r}"
        )
