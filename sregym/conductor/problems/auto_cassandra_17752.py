"""CASSANDRA-17752: fix restarting of services on gossipping-only members.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-17752
Buggy: 4.0.5   ->   Fixed: 4.0.6  (fixVersions include 4.0.6)

Reproduction (single node, nodetool sequence):
  1. Start a node with ``-Dcassandra.join_ring=false`` (a JVM ``-D`` flag, injected via
     ``JVM_EXTRA_OPTS``).  The node comes up as a gossipping-only member: gossip and the
     native/binary transport are UP, but the node never joins the ring, so it stays in the
     STARTING state forever (it never reaches NORMAL).
  2. ``nodetool disablebinary``  -> binary transport goes down (CQL is refused).
  3. ``nodetool enablebinary``   -> on buggy 4.0.5 this THROWS, because ``enablebinary`` calls
     ``StorageService#checkServiceAllowedToStart`` which only permits start in the NORMAL state.
     A join_ring=false node is in STARTING, so the check throws and the binary transport can
     never be re-enabled (the node is unrecoverable via CQL without a restart).  On fixed 4.0.6
     the same sequence succeeds and CQL is restored.

VERBATIM BUGGY SIGNATURE (from nodetool enablebinary on 4.0.5):
  nodetool: Unable to start native transport because the node is not in the normal state.

Shape: nodetool-sequence (config-gated on the join_ring=false startup flag).  This is NOT pure
CQL — the trigger and the operator-visible signature both come from ``nodetool enablebinary``,
and the join_ring=false precondition is a JVM ``-D`` flag (not a ``cassandra.yaml`` block) with
no startup crash.  So we override ``inject_fault()`` to set the startup flag, restart the node,
and run the nodetool sequence via ``kubectl exec`` (the cassandra_20108.py pattern) and log the
verbatim signature.

``continuous_reproducer`` is intentionally False (no mitigation reproducer pod is deployed). This
is a single-node *manageability* bug: only one node's binary transport gets stuck down. The
continuous-reproducer probe runs CQL against the DC-wide service ({cluster}-dc1-service) of a
3-node datacenter (the shared cluster manifest hardcodes ``size: 3``), so a SELECT would connect
to a healthy node and succeed whether or not the bug is present — a false-passing oracle. The bug
is not expressible as a cluster-wide CQL probe, so we keep only the diagnosis oracle
(LLM-as-a-judge on the root cause) and let ``inject_fault`` genuinely run the nodetool sequence
and log the verbatim signature.

NOTE: ``inject_fault`` patches the node's StatefulSet to add ``JVM_EXTRA_OPTS`` and restarts the
target pod into the gossipping-only state.  Because the K8ssandra operator manages the cluster,
the env/restart patch may be reconciled by the operator; this could not be verified at codegen
time (the task is static py_compile only, no deploy).  The buggy nodetool sequence and the
verbatim signature are taken from the authoritative reproduction evidence log.
"""

import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoCassandra17752(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.0.5"
    source_git_ref = "cassandra-4.0.5"
    # 4.0.5 already ships the bug (fix landed in 4.0.6), so deploy the stock image
    # instead of running a ~30-min ant-jar source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/service/StorageService.java"
    root_cause_description = (
        "After a node is started as a gossipping-only member (-Dcassandra.join_ring=false), "
        "nodetool enablebinary cannot re-enable the native/binary transport that was turned off "
        "by nodetool disablebinary. enablebinary calls StorageService#checkServiceAllowedToStart, "
        "which only allows the transport to start when the node is in the NORMAL state. A "
        "join_ring=false node is stuck in STARTING (it never joins the ring), so the check throws "
        "'Unable to start native transport because the node is not in the normal state.' and the "
        "node is left unreachable via CQL. The fix allows restarting services on gossipping-only members."
    )

    # Error/throw bug (NOT wrong-result): nodetool enablebinary throws and binary transport
    # stays down. No incorrect value is returned or persisted, so expected_output stays None.
    expected_output = None

    # The node starts fine and only errors at `enablebinary` time — it does NOT crash on
    # startup, so leave crash_on_startup at its False default (otherwise inject_fault would
    # wait for a CrashLoopBackOff that never happens).
    crash_on_startup = False

    # Canonical record of the buggy reproduction steps (per the evidence log). These are
    # nodetool steps run against the Cassandra SERVER pod — they are executed by the custom
    # inject_fault() below, NOT by the CQL-only run_reproducer machinery.
    reproducer = """
# Precondition: the node is started as a gossipping-only member.
#   JVM_EXTRA_OPTS=-Dcassandra.join_ring=false
# Startup log confirms: "Not joining ring as requested. Use JMX (StorageService->joinRing())
# to initiate ring joining" and `nodetool info` shows Gossip active / Native Transport active
# with Token "(node is not joined to the cluster)" — i.e. STARTING, not NORMAL.

nodetool disablebinary;
# -> binary transport goes down; cqlsh now refuses the connection; `nodetool statusbinary` => not running

nodetool enablebinary;
# -> BUGGY 4.0.5 throws:
#      nodetool: Unable to start native transport because the node is not in the normal state.
#    (thrown by StorageService#checkServiceAllowedToStart because the gossipping-only member
#     is in STARTING, not NORMAL); binary transport stays down and CQL is unrecoverable.
#    FIXED 4.0.6: enablebinary exits 0 and CQL is restored.
"""
    # Intentionally False: this single-node manageability bug is not expressible as a cluster-wide
    # CQL probe (the continuous reproducer would hit the 3-node DC service and connect to a healthy
    # node, succeeding regardless of the bug — a false-passing oracle). See the module docstring.
    # With continuous_reproducer=False the base class sets mitigation_oracle=None and we keep only
    # the diagnosis (LLM-as-a-judge) oracle; inject_fault still runs the real nodetool sequence.
    continuous_reproducer = False

    # JVM flag that puts the node into the gossipping-only (join_ring=false) state.
    _JOIN_RING_FALSE_ENV = "-Dcassandra.join_ring=false"

    def _cassandra_pod(self) -> str | None:
        """Return the name of a running Cassandra SERVER pod for kubectl exec."""
        out = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-l app.kubernetes.io/instance={self.app.cluster_name} "
            f"--no-headers -o custom-columns=NAME:.metadata.name",
            shell=True, capture_output=True, text=True,
        ).stdout
        pods = [p.strip() for p in out.splitlines() if p.strip()]
        # K8ssandra Cassandra pods are named "<cluster>-dc1-default-sts-<n>"; skip any
        # operator/stargate/reaper helper pods that may share the instance label.
        cass = [p for p in pods if "-sts-" in p] or pods
        return cass[0] if cass else None

    def _statefulset(self) -> str | None:
        """Return the Cassandra datacenter StatefulSet name."""
        out = subprocess.run(
            f"kubectl get statefulsets -n {self.namespace} "
            f"-l app.kubernetes.io/instance={self.app.cluster_name} "
            f"--no-headers -o custom-columns=NAME:.metadata.name",
            shell=True, capture_output=True, text=True,
        ).stdout
        sts = [s.strip() for s in out.splitlines() if s.strip()]
        return sts[0] if sts else None

    def _set_join_ring_false(self):
        """Patch the Cassandra StatefulSet to add JVM_EXTRA_OPTS=-Dcassandra.join_ring=false
        and restart the target pod so it comes up as a gossipping-only member.

        CAVEAT: the K8ssandra operator owns this StatefulSet and may reconcile this env patch.
        This path is the documented place for settings not expressible via the shared manifest,
        but it cannot be verified statically (no deploy at codegen time)."""
        sts = self._statefulset()
        if not sts:
            logger.warning("[AutoCassandra17752] No Cassandra StatefulSet found — cannot set join_ring=false")
            return
        patch = (
            '{"spec":{"template":{"spec":{"containers":[{"name":"cassandra",'
            f'"env":[{{"name":"JVM_EXTRA_OPTS","value":"{self._JOIN_RING_FALSE_ENV}"}}]}}]}}}}}}'
        )
        logger.info(f"[AutoCassandra17752] Setting {self._JOIN_RING_FALSE_ENV} on StatefulSet {sts}")
        subprocess.run(
            f"kubectl patch statefulset {sts} -n {self.namespace} --type=merge -p '{patch}'",
            shell=True, capture_output=True, text=True,
        )
        pod = self._cassandra_pod()
        if pod:
            logger.info(f"[AutoCassandra17752] Restarting pod {pod} into gossipping-only state")
            subprocess.run(
                f"kubectl delete pod {pod} -n {self.namespace} --grace-period=30",
                shell=True, capture_output=True, text=True,
            )
        subprocess.run(
            f"kubectl rollout status statefulset/{sts} -n {self.namespace} --timeout=300s",
            shell=True, capture_output=True, text=True,
        )

    def _exec(self, pod: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["kubectl", "exec", "-n", self.namespace, pod, "-c", "cassandra", "--", *args],
            capture_output=True, text=True,
        )

    @mark_fault_injected
    def inject_fault(self):
        """Inject CASSANDRA-17752: bring the (buggy 4.0.5) node up as a gossipping-only member,
        disable the binary transport, then fail to re-enable it via nodetool enablebinary.

        Steps (nodetool sequence on the Cassandra server pod):
          1. Ensure the buggy 4.0.5 image is active.
          2. Set JVM_EXTRA_OPTS=-Dcassandra.join_ring=false and restart -> STARTING (not NORMAL).
          3. nodetool disablebinary -> binary down.
          4. nodetool enablebinary  -> throws the verbatim signature on 4.0.5 (captured/logged).
        """
        # 1. Make sure the buggy binary is the one running. With prebuilt_from_stock=True and a
        #    released buggy version (4.0.5), the stock-deployed image already IS the buggy one,
        #    but call inject_buggy_image to match base-class semantics and be explicit.
        if not getattr(self, "_predeployed_buggy", False):
            logger.info(f"[AutoCassandra17752] Swapping cluster to buggy image {self._custom_image}")
            try:
                self.app.inject_buggy_image(self._custom_image)
            except Exception as e:
                logger.warning(f"[AutoCassandra17752] inject_buggy_image raised: {e}")
        else:
            logger.info("[AutoCassandra17752] Buggy image already deployed at cluster start — skipping swap")

        # 2. Put the node into the gossipping-only (join_ring=false) state.
        self._set_join_ring_false()

        pod = self._cassandra_pod()
        if not pod:
            logger.warning("[AutoCassandra17752] No Cassandra pod found — cannot run nodetool sequence")
        else:
            # Confirm the gossipping-only precondition took effect (guards a false negative).
            info = self._exec(pod, "nodetool", "info")
            logger.info(f"[AutoCassandra17752] nodetool info:\n{info.stdout}")

            # 3. Disable the binary transport.
            dis = self._exec(pod, "nodetool", "disablebinary")
            logger.info(f"[AutoCassandra17752] nodetool disablebinary exit={dis.returncode} {dis.stderr.strip()}")

            # 4. Attempt to re-enable it — buggy 4.0.5 throws here.
            en = self._exec(pod, "nodetool", "enablebinary")
            combined = (en.stdout + en.stderr).strip()
            if en.returncode != 0:
                logger.info(
                    f"[AutoCassandra17752] Expected buggy signature from nodetool enablebinary "
                    f"(exit={en.returncode}): {combined}"
                )
            else:
                logger.warning(
                    f"[AutoCassandra17752] nodetool enablebinary unexpectedly succeeded "
                    f"(bug may be fixed in this image): {combined}"
                )

    @mark_fault_injected
    def recover_fault(self):
        """Restore the stock image and clear the join_ring=false override so the node can
        rejoin the ring and serve CQL again."""
        sts = self._statefulset()
        if sts:
            # Remove the JVM_EXTRA_OPTS override (set value to empty) so a restart rejoins the ring.
            patch = (
                '{"spec":{"template":{"spec":{"containers":[{"name":"cassandra",'
                '"env":[{"name":"JVM_EXTRA_OPTS","value":""}]}]}}}}'
            )
            subprocess.run(
                f"kubectl patch statefulset {sts} -n {self.namespace} --type=merge -p '{patch}'",
                shell=True, capture_output=True, text=True,
            )
        logger.info("[AutoCassandra17752] Recovering: restoring cluster to stock image")
        try:
            self.app.restore_stock_image(custom_image=self._custom_image)
        except Exception as e:
            logger.warning(f"[AutoCassandra17752] restore_stock_image raised: {e}")
        logger.info("[AutoCassandra17752] Recovery complete")
