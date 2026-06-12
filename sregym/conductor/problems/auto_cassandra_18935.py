"""CASSANDRA-18935: fix nodetool enable/disablebinary to correctly set rpc (RpcReady).

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-18935
Buggy: 4.1.3   ->   Fixed: 4.1.4  (fixVersions include 4.1.4)

Reproduction (single-node-style, config-gated on a JVM ``-D`` flag + nodetool sequence):
  1. Start the node with native (binary) transport OFF at startup, via the JVM flag
     ``-Dcassandra.start_native_transport=false`` injected through ``JVM_EXTRA_OPTS``.
     ``cassandra.yaml`` keeps ``start_native_transport: true``, so CassandraDaemon.setup()
     still constructs the nativeTransportService (required for ``enablebinary`` to work),
     while CassandraDaemon.start() skips the ``startNativeTransport(); setRpcReady(true);``
     branch. The startup log confirms: "Not starting native transport as requested. Use JMX
     (StorageService->startNativeTransport()) or nodetool (enablebinary) to start it".
  2. ``nodetool enablebinary``  -> native (binary) transport starts and ``nodetool statusbinary``
     reports "running", but ``StorageService.setRpcReady(true)`` is NEVER called (the bug).
  3. A plain (non-counter) INSERT succeeds (the node is otherwise healthy), but a counter
     UPDATE fails: since CASSANDRA-13043 a counter update requires RpcReady=true to select a
     counter leader, and with RpcReady never set no replica is counted as "alive".

Root cause (CassandraDaemon.java startup if-block — quoted from the Jira body):
    if ((nativeFlag != null && Boolean.parseBoolean(nativeFlag))
            || (nativeFlag == null && DatabaseDescriptor.startNativeTransport())) {
        startNativeTransport();
        StorageService.instance.setRpcReady(true);
    }
``setRpcReady(true)`` only runs when native transport is enabled AT STARTUP. Starting with
native OFF and later running ``nodetool enablebinary`` starts the transport but leaves
RpcReady=false forever. The fix moves ``setRpcReady(true)`` out of this startup ``if`` (and into
enable/disablebinary) so toggling the binary transport correctly updates RpcReady.

VERBATIM BUGGY SIGNATURE (literal cqlsh output of the counter UPDATE on buggy 4.1.3):
  <stdin>:1:NoHostAvailable: ('Unable to complete the operation against any hosts', {<Host: 127.0.0.1:9042 dc1>: Unavailable('Error from server: code=1000 [Unavailable exception] message="Cannot achieve consistency level ONE" info={\\'consistency\\': \\'ONE\\', \\'required_replicas\\': 1, \\'alive_replicas\\': 0}')})

Note ``alive_replicas: 0`` while ``required_replicas: 1`` on an otherwise healthy ring with
RF=1: no replica is counted as RPC-ready for counter-leader selection — the precise symptom of
RpcReady never being set. On fixed 4.1.4 the IDENTICAL gating + nodetool sequence makes the
counter UPDATE succeed (the counter increments to c=1).

Shape: nodetool-sequence (config-gated on the ``start_native_transport=false`` startup JVM
``-D`` flag — NOT a ``cassandra.yaml`` block, and NOT a startup crash). The gating precondition
must be present at process startup, the trigger and operator-visible signature come from running
``nodetool enablebinary`` followed by a counter UPDATE, and none of that is expressible via the
shared CQL-only ``reproducer`` machinery. So we override ``inject_fault()`` to set the startup
flag on ALL Cassandra nodes, restart them, run ``nodetool enablebinary`` on every node, seed the
schema + a plain write, and then deploy a continuous reproducer that loops ONLY the counter
UPDATE (the cassandra_20108 + auto_cassandra_17752 kubectl-exec pattern).

``continuous_reproducer`` is True. Unlike the closely-related CASSANDRA-17752 (join_ring=false),
gating native transport OFF does NOT prevent the node from joining the ring: the node reaches the
NORMAL state, only CQL/native is off at startup. Gating ALL nodes therefore yields a HEALTHY ring
in which cqlsh connects and plain writes succeed, but counter UPDATEs fail on EVERY coordinator
(RpcReady=false everywhere). The DC-wide continuous-reproducer probe is thus a correct oracle (it
fails uniformly while the bug is present), not the false-passing probe that forced 17752 to
disable it. This requires inject_fault to gate every node (see the all-nodes loops below).
"""

import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoCassandra18935(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.1.3"
    source_git_ref = "cassandra-4.1.3"
    # 4.1.3 already ships the bug (fix landed in 4.1.4), so deploy the stock image
    # instead of running a ~30-min ant-jar source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/service/CassandraDaemon.java"
    root_cause_description = (
        "CassandraDaemon only calls StorageService.setRpcReady(true) inside the startup if-block "
        "that starts native transport (gated on -Dcassandra.start_native_transport / "
        "DatabaseDescriptor.startNativeTransport()). If the node is started with native transport "
        "OFF and the binary transport is later turned on with `nodetool enablebinary`, the native "
        "transport starts but setRpcReady(true) is never called, so RpcReady stays false. Since "
        "CASSANDRA-13043 a counter update requires RpcReady=true to select a counter leader, so the "
        "counter UPDATE fails to find any RPC-ready (alive) replica and reports 'Cannot achieve "
        "consistency level ONE' with alive_replicas: 0. The fix makes nodetool enable/disablebinary "
        "correctly set RpcReady instead of leaving it pinned to its startup value."
    )

    # Error/throw bug (NOT wrong-result): the counter UPDATE fails with NoHostAvailable /
    # Unavailable ("Cannot achieve consistency level ONE", alive_replicas: 0). No incorrect value
    # is returned or persisted, so expected_output stays None. With expected_output=None the
    # ReproducerPodMitigationOracle uses expect_unready=False -> NotReady = bug present.
    expected_output = None

    # The node starts fine (native transport is simply off until enablebinary); it does NOT crash
    # on startup, so leave crash_on_startup at its False default (otherwise inject_fault would wait
    # for a CrashLoopBackOff that never happens).
    crash_on_startup = False

    # The bug fails uniformly across all coordinators once every node is gated native-OFF, so the
    # DC-wide CQL probe is a correct oracle. Deploy the continuous reproducer (counter UPDATE loop).
    continuous_reproducer = True

    # JVM flag that starts the node with native (binary) transport OFF so the buggy startup path is
    # taken (setRpcReady(true) skipped) while the nativeTransportService is still constructed.
    _NATIVE_OFF_ENV = "-Dcassandra.start_native_transport=false"

    # Keyspace / tables (mirrors the evidence-log buggy run: keyspace repro18935_ks, counter table
    # `cnt` with counter column `c`, plain table `plain`). RF=1 keeps the counter-leader symptom
    # (required_replicas: 1, alive_replicas: 0) on the otherwise-healthy ring.
    _KEYSPACE = "repro18935_ks"
    _SETUP_CQL = (
        "CREATE KEYSPACE IF NOT EXISTS repro18935_ks "
        "WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};\n"
        "CREATE TABLE IF NOT EXISTS repro18935_ks.plain (k text PRIMARY KEY, v text);\n"
        "CREATE TABLE IF NOT EXISTS repro18935_ks.cnt (k text PRIMARY KEY, c counter);\n"
        "INSERT INTO repro18935_ks.plain (k, v) VALUES ('a', 'hello');\n"
    )
    # Pure-CQL counter UPDATE looped by the continuous reproducer pod. Fully-qualified (no USE):
    # _strip_sql_db_setup strips USE statements but not CREATE KEYSPACE, so the loop string is
    # self-contained. This is the statement that fails on buggy 4.1.3 and succeeds on fixed 4.1.4.
    _COUNTER_UPDATE = (
        "CREATE KEYSPACE IF NOT EXISTS repro18935_ks "
        "WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};\n"
        "CREATE TABLE IF NOT EXISTS repro18935_ks.cnt (k text PRIMARY KEY, c counter);\n"
        "UPDATE repro18935_ks.cnt SET c = c + 1 WHERE k = 'a';\n"
    )

    # Canonical record of the buggy reproduction steps (per the evidence log). These are nodetool
    # steps run against the Cassandra SERVER pods plus the CQL trigger — they are executed by the
    # custom inject_fault() below, NOT by the CQL-only run_reproducer machinery. The continuous
    # loop uses _COUNTER_UPDATE (pure CQL) instead of this annotated block.
    reproducer = """
# Precondition: every Cassandra node is started with native (binary) transport OFF.
#   JVM_EXTRA_OPTS=-Dcassandra.start_native_transport=false
# (cassandra.yaml keeps start_native_transport: true, so setup() still builds the
#  nativeTransportService.) Startup log confirms:
#   "Not starting native transport as requested. Use JMX
#    (StorageService->startNativeTransport()) or nodetool (enablebinary) to start it"
#   `nodetool statusbinary` => not running

nodetool enablebinary;
# -> native transport starts; `nodetool statusbinary` => running.
#    BUT StorageService.setRpcReady(true) is NEVER called (the bug) -> RpcReady stays false.

# Plain (non-counter) write SUCCEEDS — the node is healthy for normal writes:
INSERT INTO repro18935_ks.plain (k, v) VALUES ('a', 'hello');
SELECT * FROM repro18935_ks.plain;

# >>>> COUNTER UPDATE (BUG TRIGGER) — fails on buggy 4.1.3 <<<<
UPDATE repro18935_ks.cnt SET c = c + 1 WHERE k = 'a';
# -> BUGGY 4.1.3:
#      <stdin>:1:NoHostAvailable: ('Unable to complete the operation against any hosts',
#        {<Host: 127.0.0.1:9042 dc1>: Unavailable('Error from server: code=1000
#         [Unavailable exception] message="Cannot achieve consistency level ONE"
#         info={'consistency': 'ONE', 'required_replicas': 1, 'alive_replicas': 0})})
#    (no replica counted as RPC-ready for counter-leader selection because RpcReady was
#     never set). FIXED 4.1.4: the counter UPDATE succeeds and c increments to 1.
"""

    # ── Pod / StatefulSet discovery (ALL nodes — the bug requires gating every node) ───────────

    def _cassandra_pods(self) -> list[str]:
        """Return the names of ALL running Cassandra SERVER pods for kubectl exec."""
        out = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-l app.kubernetes.io/instance={self.app.cluster_name} "
            f"--no-headers -o custom-columns=NAME:.metadata.name",
            shell=True, capture_output=True, text=True,
        ).stdout
        pods = [p.strip() for p in out.splitlines() if p.strip()]
        # K8ssandra Cassandra pods are named "<cluster>-dc1-default-sts-<n>"; skip any
        # operator/stargate/reaper helper pods that may share the instance label.
        cass = [p for p in pods if "-sts-" in p]
        return cass or pods

    def _statefulsets(self) -> list[str]:
        """Return ALL Cassandra datacenter StatefulSet names."""
        out = subprocess.run(
            f"kubectl get statefulsets -n {self.namespace} "
            f"-l app.kubernetes.io/instance={self.app.cluster_name} "
            f"--no-headers -o custom-columns=NAME:.metadata.name",
            shell=True, capture_output=True, text=True,
        ).stdout
        return [s.strip() for s in out.splitlines() if s.strip()]

    def _set_native_off_all(self):
        """Patch EVERY Cassandra StatefulSet to add JVM_EXTRA_OPTS=-Dcassandra.start_native_transport=false
        and rollout-restart them so all nodes come up with native transport OFF at startup.

        CAVEAT: the K8ssandra operator owns these StatefulSets and may reconcile this env patch.
        This is the documented place for settings not expressible via the shared manifest, but it
        cannot be verified statically (no deploy at codegen time). It is essential that ALL nodes
        are gated: if any node started native-ON its coordinator would still have RpcReady=true and
        the counter UPDATE could succeed while the bug is present (a false-passing oracle)."""
        stss = self._statefulsets()
        if not stss:
            logger.warning("[AutoCassandra18935] No Cassandra StatefulSet found — cannot set start_native_transport=false")
            return
        patch = (
            '{"spec":{"template":{"spec":{"containers":[{"name":"cassandra",'
            f'"env":[{{"name":"JVM_EXTRA_OPTS","value":"{self._NATIVE_OFF_ENV}"}}]}}]}}}}'
        )
        for sts in stss:
            logger.info(f"[AutoCassandra18935] Setting {self._NATIVE_OFF_ENV} on StatefulSet {sts}")
            subprocess.run(
                f"kubectl patch statefulset {sts} -n {self.namespace} --type=merge -p '{patch}'",
                shell=True, capture_output=True, text=True,
            )
            subprocess.run(
                f"kubectl rollout restart statefulset/{sts} -n {self.namespace}",
                shell=True, capture_output=True, text=True,
            )
        for sts in stss:
            subprocess.run(
                f"kubectl rollout status statefulset/{sts} -n {self.namespace} --timeout=600s",
                shell=True, capture_output=True, text=True,
            )

    def _exec(self, pod: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["kubectl", "exec", "-n", self.namespace, pod, "-c", "cassandra", "--", *args],
            capture_output=True, text=True,
        )

    @mark_fault_injected
    def inject_fault(self):
        """Inject CASSANDRA-18935: bring every (buggy 4.1.3) node up with native transport OFF,
        re-enable it with `nodetool enablebinary` (which leaves RpcReady=false), seed the schema +
        a plain write, then loop the counter UPDATE that fails on the buggy build.

        Steps:
          1. Ensure the buggy 4.1.3 image is active.
          2. Set JVM_EXTRA_OPTS=-Dcassandra.start_native_transport=false on ALL nodes and restart.
          3. Run `nodetool enablebinary` on EVERY node -> native transport up, RpcReady still false.
          4. Seed the keyspace/tables + a plain (non-counter) INSERT (succeeds).
          5. Fire the counter UPDATE once (logs the verbatim Unavailable signature) and deploy the
             continuous reproducer that loops ONLY the counter UPDATE.
        """
        # 1. Make sure the buggy binary is the one running. With prebuilt_from_stock=True and a
        #    released buggy version (4.1.3), the stock-deployed image already IS the buggy one,
        #    but call inject_buggy_image to match base-class semantics and be explicit.
        if not getattr(self, "_predeployed_buggy", False):
            logger.info(f"[AutoCassandra18935] Swapping cluster to buggy image {self._custom_image}")
            try:
                self.app.inject_buggy_image(self._custom_image)
            except Exception as e:
                logger.warning(f"[AutoCassandra18935] inject_buggy_image raised: {e}")
        else:
            logger.info("[AutoCassandra18935] Buggy image already deployed at cluster start — skipping swap")

        # 2. Bring every node up with native transport OFF at startup (the buggy startup path).
        self._set_native_off_all()

        pods = self._cassandra_pods()
        if not pods:
            logger.warning("[AutoCassandra18935] No Cassandra pods found — cannot run nodetool sequence")
        else:
            # 3. Re-enable the binary transport on EVERY node. On buggy 4.1.3 this succeeds but
            #    leaves RpcReady=false (the bug); confirm statusbinary as a guard against a false
            #    negative (e.g. native already on because the env patch was reconciled away).
            for pod in pods:
                en = self._exec(pod, "nodetool", "enablebinary")
                logger.info(
                    f"[AutoCassandra18935] {pod} nodetool enablebinary exit={en.returncode} "
                    f"{(en.stdout + en.stderr).strip()}"
                )
                status = self._exec(pod, "nodetool", "statusbinary")
                logger.info(f"[AutoCassandra18935] {pod} nodetool statusbinary: {status.stdout.strip()}")

        # 4. Seed the schema and a plain write (the plain write succeeds — proves the ring is healthy).
        logger.info("[AutoCassandra18935] Seeding keyspace/tables + plain write")
        try:
            self.app.run_reproducer(self._SETUP_CQL)
        except Exception as e:
            logger.warning(f"[AutoCassandra18935] setup CQL raised: {e}")

        # 5. Fire the counter UPDATE once to surface/log the verbatim Unavailable signature, then
        #    deploy the continuous reproducer that loops ONLY the counter UPDATE.
        logger.info("[AutoCassandra18935] Firing counter UPDATE (expect Unavailable on buggy 4.1.3)")
        try:
            self.app.run_reproducer(self._COUNTER_UPDATE)
        except Exception as e:
            logger.info(f"[AutoCassandra18935] Expected counter UPDATE failure: {e}")

        logger.info("[AutoCassandra18935] Deploying continuous counter-UPDATE reproducer")
        self.app.deploy_continuous_reproducer(self._COUNTER_UPDATE, self.expected_output)

    @mark_fault_injected
    def recover_fault(self):
        """Restore the stock image and clear the start_native_transport=false override on all nodes
        so they come back up with native transport (and RpcReady) normal."""
        for sts in self._statefulsets():
            # Remove the JVM_EXTRA_OPTS override (set value to empty) so a restart starts native
            # transport at startup and sets RpcReady=true.
            patch = (
                '{"spec":{"template":{"spec":{"containers":[{"name":"cassandra",'
                '"env":[{"name":"JVM_EXTRA_OPTS","value":""}]}]}}}}'
            )
            subprocess.run(
                f"kubectl patch statefulset {sts} -n {self.namespace} --type=merge -p '{patch}'",
                shell=True, capture_output=True, text=True,
            )
        logger.info("[AutoCassandra18935] Recovering: restoring cluster to stock image")
        try:
            self.app.restore_stock_image(custom_image=self._custom_image)
        except Exception as e:
            logger.warning(f"[AutoCassandra18935] restore_stock_image raised: {e}")
        logger.info("[AutoCassandra18935] Recovery complete")
