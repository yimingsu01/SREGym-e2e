"""Zero length file in audit log folder prevents a node from starting.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-17933

Title: Zero length file in Audit log folder, prevents a node from starting.

Buggy: 4.0.6.  Fixed: 4.0.7 (also 4.1-rc1, 4.1, 5.0-alpha1, 5.0).

Reproduction summary (config-gated, single node, startup failure):
  1. Enable audit logging in cassandra.yaml (audit_logging_options.enabled: true,
     default logger BinAuditLogger) pointed at a PVC-backed audit_logs_dir.
  2. Plant a zero-byte .cq4 file (e.g. 20220928-12.cq4) in that audit directory.
  3. Start the node. Startup aborts during AuditLogManager static initialization:
     when BinAuditLogger/BinLog opens the chronicle queue, chronicle-queue's
     SingleChronicleQueue.cleanupStoreFilesWithNoData tries to lock/resize the
     empty file and throws OverlappingFileLockException, wrapped as
     ExceptionInInitializerError. The node never reaches CQL (exitCode=3).
  The fix (4.0.7) cleans up zero-byte chronicle files instead of aborting.

Verbatim buggy signature (from the reproduction evidence log):
  ERROR [main] CassandraDaemon.java:911 - Exception encountered during startup
  java.lang.ExceptionInInitializerError: null
    at org.apache.cassandra.service.CassandraDaemon.setup(CassandraDaemon.java:468)
    ...
  Caused by: org.apache.cassandra.exceptions.ConfigurationException: Unable to create instance of IAuditLogger.
    at org.apache.cassandra.utils.FBUtilities.newAuditLogger(FBUtilities.java:686)
    at org.apache.cassandra.audit.AuditLogManager.getAuditLogger(AuditLogManager.java:95)
    at org.apache.cassandra.audit.AuditLogManager.<init>(AuditLogManager.java:74)
    at org.apache.cassandra.audit.AuditLogManager.<clinit>(AuditLogManager.java:60)
  Caused by: java.lang.reflect.InvocationTargetException: null
    ...
  Caused by: java.nio.channels.OverlappingFileLockException: null
    at java.base/sun.nio.ch.FileLockTable.checkList(Unknown Source)
    ...
    at net.openhft.chronicle.bytes.MappedFile.resizeRafIfTooSmall(MappedFile.java:369)
    ...
    at net.openhft.chronicle.queue.impl.single.SingleChronicleQueue.cleanupStoreFilesWithNoData(SingleChronicleQueue.java:821)
    ...
    at org.apache.cassandra.utils.binlog.BinLog.<init>(BinLog.java:133)
    at org.apache.cassandra.utils.binlog.BinLog.<init>(BinLog.java:65)
    at org.apache.cassandra.utils.binlog.BinLog$Builder.build(BinLog.java:453)
    at org.apache.cassandra.audit.BinAuditLogger.<init>(BinAuditLogger.java:55)
"""

import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem

logger = logging.getLogger(__name__)

# PVC-backed audit directory (the K8ssandra data volume is mounted at /var/lib/cassandra,
# which survives the operator's rolling restart) and the planted zero-byte chronicle file
# (matches the Jira body's `-rw-rw-r--. 1 ... 0 Sep 28 13:00 20220928-12.cq4`).
_AUDIT_DIR = "/var/lib/cassandra/audit"
_ZERO_BYTE_CQ4 = f"{_AUDIT_DIR}/20220928-12.cq4"


class AutoCassandra17933(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.0.6"
    source_git_ref = "cassandra-4.0.6"
    # 4.0.6 already ships the bug (fix landed in 4.0.7), so deploy/re-tag the stock image
    # instead of an ~30-min ant-jar build.
    prebuilt_from_stock = True

    # Deepest Cassandra frame before control passes into the third-party chronicle-queue
    # library; the 4.0.7 fix added zero-byte chronicle-file cleanup on this path.
    root_cause_file = "src/java/org/apache/cassandra/utils/binlog/BinLog.java"
    root_cause_description = (
        "With audit logging enabled (default BinAuditLogger), a zero-byte .cq4 file left in the "
        "audit_logs_dir prevents the node from starting. During AuditLogManager static "
        "initialization, BinAuditLogger/BinLog opens the chronicle queue and chronicle-queue's "
        "SingleChronicleQueue.cleanupStoreFilesWithNoData attempts to lock/resize the empty file, "
        "throwing java.nio.channels.OverlappingFileLockException. It surfaces as "
        "ExceptionInInitializerError from AuditLogManager.<clinit> (wrapped as ConfigurationException: "
        "'Unable to create instance of IAuditLogger') and aborts startup with exit code 3. The fix "
        "(4.0.7) detects and cleans up the empty chronicle file instead of failing startup."
    )

    # This is a config-gated STARTUP-CRASH bug, not a query-time bug. The crash_on_startup
    # branch in GenericCustomBuildProblem.inject_fault() runs setup_preconditions() (below)
    # while the stock binary is still up, then swaps in the buggy image and waits for the
    # node to enter CrashLoopBackOff — it never executes `reproducer` as CQL. The string
    # below therefore documents the manual reproduction steps rather than runnable CQL.
    reproducer = """
-- CASSANDRA-17933 reproduction (config-gated startup failure; NOT executable CQL).
-- Audit logging is enabled via the K8ssandraCluster CR (post_deploy), the zero-byte
-- file is planted on the PVC (setup_preconditions), then the buggy-image swap restarts
-- the node, which trips on the empty chronicle file during startup.
--
-- 1. Enable audit logging in cassandra.yaml (default logger BinAuditLogger):
--      audit_logging_options:
--        enabled: true
--        audit_logs_dir: /var/lib/cassandra/audit
-- 2. Plant a zero-byte .cq4 file in the audit directory:
--      mkdir -p /var/lib/cassandra/audit
--      : > /var/lib/cassandra/audit/20220928-12.cq4   # 0 bytes
-- 3. (Re)start the node. Startup aborts during AuditLogManager.<clinit> with
--    java.nio.channels.OverlappingFileLockException from
--    SingleChronicleQueue.cleanupStoreFilesWithNoData; the node never reaches CQL.
"""

    # Startup-crash bug: inject runs preconditions on the stock binary, swaps to the buggy
    # image, and waits for CrashLoopBackOff rather than a Ready pod.
    crash_on_startup = True
    # Diagnosis-only. The crash_on_startup branch of inject_fault() returns before
    # deploy_continuous_reproducer(), so the {cluster_name}-reproducer Deployment that
    # ReproducerPodMitigationOracle inspects is never created — a mitigation oracle would
    # hit its 404 branch (success=False) for BOTH the buggy and the fixed build and could
    # not discriminate. Leaving continuous_reproducer False makes mitigation_oracle = None
    # (diagnosis graded by LLMAsAJudgeOracle on the root cause), matching the etcd/tidb
    # crash_on_startup precedents. No expected_output (this is a crash, not a wrong result).
    continuous_reproducer = False

    def post_deploy(self):
        """Enable audit logging on the deployed cluster via the K8ssandraCluster CR.

        Audit logging (audit_logging_options.enabled) is a cassandra.yaml STARTUP setting,
        disabled by default in 4.0.x. It must be enabled through the operator-owned CR
        cassandraYaml block — NOT by editing cassandra.yaml inside the running pod, because
        /opt/cassandra/conf is on the container's ephemeral filesystem and the operator
        re-renders cassandra.yaml from the CR on every reconcile / image swap. Patching the
        CR makes audit logging persist across the buggy-image rolling restart, which is what
        actually constructs BinAuditLogger at startup and trips the empty chronicle file.

        audit_logs_dir points at a PVC-backed path (/var/lib/cassandra/audit) so the
        zero-byte file planted by setup_preconditions() is the file the restarted node reads.
        """
        cluster = self.app.cluster_name
        ns = self.namespace
        logger.info(
            f"[AutoCassandra17933] Enabling audit logging on K8ssandraCluster '{cluster}' "
            f"in {ns} (cassandraYaml.audit_logging_options.enabled=true, "
            f"audit_logs_dir={_AUDIT_DIR})"
        )
        # Cluster-level cassandraYaml passthrough (applies to all datacenters); audit_logging_options
        # is a structured cassandra.yaml key, so it is supplied as a nested object.
        patch = (
            '{"spec":{"cassandra":{"config":{"cassandraYaml":'
            '{"audit_logging_options":{"enabled":true,'
            f'"audit_logs_dir":"{_AUDIT_DIR}"' + '}}}}}}'
        )
        result = subprocess.run(
            f"kubectl patch k8ssandracluster {cluster} -n {ns} "
            f"--type=merge -p '{patch}'",
            shell=True, capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.warning(
                f"[AutoCassandra17933] enable audit_logging_options patch failed: "
                f"{result.stderr.strip()[:300]}"
            )
            return

        # The patch triggers a rolling restart of the datacenter; wait for it to settle so
        # setup_preconditions() plants the file against a node that already has audit-logging
        # configured (the file goes onto the PVC, surviving the later buggy-image restart).
        subprocess.run(
            f"kubectl wait --for=condition=Ready k8ssandracluster/{cluster} "
            f"-n {ns} --timeout=600s",
            shell=True, capture_output=True, text=True,
        )
        logger.info("[AutoCassandra17933] Audit logging enabled and cluster Ready")

    def setup_preconditions(self):
        """Plant a zero-byte .cq4 file in every Cassandra pod's PVC-backed audit directory
        while the stock binary is still running.

        Audit logging is already enabled via the CR (post_deploy). The subsequent buggy-image
        swap (inject_buggy_image_expect_crash) restarts the node, which then aborts during
        AuditLogManager static initialization when chronicle-queue tries to lock/resize this
        empty file. The file lives under /var/lib/cassandra (the data PVC), so it persists
        across the restart.
        """
        pods = self._cassandra_pods()
        if not pods:
            logger.warning(
                "[AutoCassandra17933] No Cassandra pods found in namespace "
                f"{self.namespace!r} — cannot plant zero-byte audit file"
            )
            return

        # Create the audit dir and plant the zero-byte chronicle file on the PVC.
        plant_script = (
            f"mkdir -p {_AUDIT_DIR}; "
            f": > {_ZERO_BYTE_CQ4}; "
            f"ls -l {_AUDIT_DIR}"
        )

        for pod in pods:
            logger.info(
                f"[AutoCassandra17933] Planting zero-byte {_ZERO_BYTE_CQ4} on pod {pod}"
            )
            cmd = (
                f"kubectl exec -n {self.namespace} {pod} -c cassandra -- "
                f"bash -c {subprocess.list2cmdline([plant_script])}"
            )
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode != 0:
                logger.warning(
                    f"[AutoCassandra17933] Planting file on {pod} returned "
                    f"{result.returncode}: {result.stderr.strip()}"
                )
            else:
                logger.info(
                    f"[AutoCassandra17933] Audit dir on {pod}:\n{result.stdout.strip()}"
                )

    def _cassandra_pods(self) -> list[str]:
        """Return the Cassandra StatefulSet pod names for this cluster."""
        out = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-l app.kubernetes.io/managed-by=cass-operator "
            f"-o jsonpath='{{.items[*].metadata.name}}'",
            shell=True, capture_output=True, text=True,
        ).stdout.strip().strip("'")
        pods = [p for p in out.split() if p]
        if pods:
            return pods
        # Fallback selector if the managed-by label differs across operator versions.
        out = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-l app.kubernetes.io/name=cassandra "
            f"-o jsonpath='{{.items[*].metadata.name}}'",
            shell=True, capture_output=True, text=True,
        ).stdout.strip().strip("'")
        return [p for p in out.split() if p]
