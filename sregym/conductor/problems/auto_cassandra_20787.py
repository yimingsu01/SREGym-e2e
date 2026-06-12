"""Cassandra crashes on first boot with data_disk_usage_max_disk_size set when the data
directory is not yet created.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-20787

Title: Startup fails with data_disk_usage_max_disk_size guardrail when the data directory
does not exist yet (fresh node).

Buggy: 5.0.4.  Fixed: 4.1.10, 5.0.5, 6.0-alpha1, 6.0.

Reproduction summary (config-gated, single node, startup failure):
  1. Set data_disk_usage_max_disk_size to any value (e.g. 1GiB) in cassandra.yaml
     (default is commented out / null, so the guardrail is normally inert).
  2. Start a FRESH node whose data directory does not exist yet.
  3. DatabaseDescriptor.applyGuardrails() runs BEFORE createAllDirectories(), so
     GuardrailsOptions.<init> -> validateDataDiskUsageMaxDiskSize() ->
     DiskUsageMonitor.totalDiskSpace() -> dataDirectoriesGroupedByFileStore() ->
     Files.getFileStore(<data dir>) is invoked while the data dir is still absent.
     getFileStore throws NoSuchFileException and startup aborts (exit code 3,
     CrashLoopBackOff). The fix reorders so createAllDirectories() runs first (5.0.5
     boots cleanly under the identical config and precondition, having created the data
     dir before guardrail validation).

Verbatim buggy signature (from the reproduction evidence log; the exception thrown on
startup, on every boot attempt):
  Exception (java.lang.RuntimeException) encountered during startup: Cannot get data
  directories grouped by file store
  java.lang.RuntimeException: Cannot get data directories grouped by file store
      at org.apache.cassandra.service.disk.usage.DiskUsageMonitor.dataDirectoriesGroupedByFileStore(DiskUsageMonitor.java:202)
      at org.apache.cassandra.service.disk.usage.DiskUsageMonitor.totalDiskSpace(DiskUsageMonitor.java:209)
      at org.apache.cassandra.config.GuardrailsOptions.validateDataDiskUsageMaxDiskSize(GuardrailsOptions.java:1255)
      at org.apache.cassandra.config.GuardrailsOptions.<init>(GuardrailsOptions.java:87)
      at org.apache.cassandra.config.DatabaseDescriptor.applyGuardrails(DatabaseDescriptor.java:1113)
      at org.apache.cassandra.config.DatabaseDescriptor.applyAll(DatabaseDescriptor.java:470)
      at org.apache.cassandra.config.DatabaseDescriptor.daemonInitialization(DatabaseDescriptor.java:262)
      ...
  Caused by: java.nio.file.NoSuchFileException: /opt/cassandra/data/data
      ...
  ERROR [main] CassandraDaemon.java:887 - Exception encountered during startup
  (followed by JVM exit code 3 / pod CrashLoopBackOff)

Runtime-fidelity note: the evidence log reproduced this on a single bare `cassandra:5.0.4`
pod (data dir absent in the image, entrypoint does not create it) so first boot satisfied
the bug precondition. The SREGym runtime instead deploys a 3-node K8ssandra-operator cluster
whose STOCK 5.0.4 nodes boot first and CREATE the PVC-backed data dir
(/var/lib/cassandra/data) before any fault is injected. So unlike the bare-image case, the
data dir already exists by the time the buggy image is swapped in. To recreate the "fresh
node" precondition the bug actually requires, the data dir is therefore:
  - configured via the operator-owned CR (post_deploy sets the guardrail in cassandraYaml so
    it survives the operator's config re-render on the rolling restart), and
  - DELETED on each pod's data PVC in setup_preconditions() immediately before the
    buggy-image swap (the analog of arming the trigger).
The buggy node then restarts over a PVC whose data dir is gone, hits guardrail validation
before createAllDirectories(), and crashes — exactly as in the evidence log. The bug is
single-node by nature (a per-node startup ordering bug); the 3-node cluster is incidental
infrastructure.

Residual risk (cannot be settled statically, deploy is forbidden): if the
k8ssandra/cass-management-api entrypoint pre-creates /var/lib/cassandra/data before launching
Cassandra, OR if the still-running stock node recreates the dir between the deletion and the
buggy-image restart, the data dir would be present again before guardrail validation and the
crash would not fire. The evidence log verified the BARE cassandra:5.0.4 image's entrypoint
does NOT create the data dir, but the management-api image is a different entrypoint. There is
no better static hook than deleting the dir in setup_preconditions() immediately before the
swap (the analog of 21290's truncate-right-before-swap); this is the one piece encoded
best-effort. See the deletion step below.
"""

import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem

logger = logging.getLogger(__name__)

# data_file_directories[0] on the K8ssandra cass-management-api image is the PVC-backed
# /var/lib/cassandra/data (established by CASSANDRA-21290, whose heartbeat path
# /var/lib/cassandra/data/cassandra-heartbeat = getLocalSystemKeyspacesDataFileLocations()[0]
# /cassandra-heartbeat). The mount /var/lib/cassandra always exists; only its `data` child is
# the data dir whose absence trips the guardrail. This matches the evidence log exactly:
# `ls: cannot access '/var/lib/cassandra/data'` while `/var/lib/cassandra` is present.
# (The evidence log's NoSuchFileException path /opt/cassandra/data/data is the BARE image's
# default data dir; on the management-api image the configured dir is the PVC path below.)
_DATA_DIR = "/var/lib/cassandra/data"


class AutoCassandra20787(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "5.0.4"
    source_git_ref = "cassandra-5.0.4"
    # 5.0.4 already ships the bug (fix landed in 5.0.5), so deploy/re-tag the stock image
    # instead of an ~30-min ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/config/DatabaseDescriptor.java"
    root_cause_description = (
        "Cassandra 5.0.4 aborts startup when data_disk_usage_max_disk_size is set in "
        "cassandra.yaml and the data directory does not yet exist (a fresh node). The root "
        "cause is an initialization ORDERING bug in DatabaseDescriptor: applyGuardrails() "
        "(via applyAll() -> daemonInitialization()) runs BEFORE createAllDirectories(), so "
        "the guardrail validation happens while the data dir is still absent. With "
        "data_disk_usage_max_disk_size set, GuardrailsOptions.<init> calls "
        "validateDataDiskUsageMaxDiskSize(), which goes DiskUsageMonitor.totalDiskSpace() -> "
        "DiskUsageMonitor.dataDirectoriesGroupedByFileStore() -> "
        "Files.getFileStore(<data dir>). On a fresh node the data dir does not exist, so "
        "Files.getFileStore throws java.nio.file.NoSuchFileException, surfaced as "
        "RuntimeException 'Cannot get data directories grouped by file store', and startup "
        "aborts with exit code 3 (CrashLoopBackOff). The fix (4.1.10/5.0.5/6.0) ensures the "
        "data directories are created (createAllDirectories) before the guardrail validation "
        "runs, so getFileStore sees an existing directory; the fixed build boots cleanly under "
        "the identical config and precondition."
    )

    # This is a config-gated STARTUP-CRASH bug, not a query-time bug. The crash_on_startup
    # branch in GenericCustomBuildProblem.inject_fault() runs setup_preconditions() (below)
    # while the stock binary is still up, then swaps in the buggy image and waits for the
    # node to enter CrashLoopBackOff — it never executes `reproducer` as CQL. The string below
    # therefore documents the manual reproduction steps rather than runnable CQL.
    reproducer = """
-- CASSANDRA-20787 reproduction (config-gated startup failure; NOT executable CQL).
-- crash_on_startup=True: inject_fault() runs setup_preconditions() on the stock binary,
-- then swaps in the buggy image and waits for CrashLoopBackOff. This string is never run
-- as CQL; it documents the steps the framework hooks perform.
--
-- 1. Set data_disk_usage_max_disk_size to any value in cassandra.yaml (default is
--    commented out / null, so the guardrail is normally inert). Applied via the
--    K8ssandraCluster CR cassandraYaml block (post_deploy):
--      data_disk_usage_max_disk_size: 1GiB
-- 2. Make the node's data directory absent (the "fresh node" precondition). The stock
--    cluster has already created it on the PVC, so setup_preconditions() deletes it
--    immediately before the buggy-image swap:
--      rm -rf /var/lib/cassandra/data
-- 3. (Re)start the node onto the buggy 5.0.4 image. DatabaseDescriptor.applyGuardrails()
--    runs before createAllDirectories(), so GuardrailsOptions.<init> ->
--    validateDataDiskUsageMaxDiskSize -> DiskUsageMonitor.dataDirectoriesGroupedByFileStore
--    -> Files.getFileStore(<data dir>) is invoked while the data dir is gone, throwing
--    NoSuchFileException -> RuntimeException "Cannot get data directories grouped by file
--    store"; the node never reaches CQL (exitCode=3, CrashLoopBackOff).
--
-- A/B control (cross-version, per the evidence log): the fixed image cassandra:5.0.5 runs the
-- IDENTICAL config under the IDENTICAL fresh-data-dir precondition, boots cleanly, serves
-- cqlsh, and creates the data dir BEFORE guardrail validation — confirming the ordering fix.
"""

    # Startup-crash bug: inject runs preconditions on the stock binary, swaps to the buggy
    # image, and waits for CrashLoopBackOff rather than a Ready pod.
    crash_on_startup = True
    # Diagnosis-only. The crash_on_startup branch of inject_fault() returns before
    # deploy_continuous_reproducer(), so the {cluster_name}-reproducer Deployment that
    # ReproducerPodMitigationOracle inspects is never created — a mitigation oracle would hit
    # its 404 branch (success=False) for BOTH the buggy and the fixed build and could not
    # discriminate. Leaving continuous_reproducer False makes mitigation_oracle = None
    # (diagnosis graded by LLMAsAJudgeOracle on the root cause), matching the
    # auto_cassandra_21290 / auto_cassandra_17933 crash_on_startup precedents. No
    # expected_output (this is a crash, not a wrong result).
    continuous_reproducer = False

    def post_deploy(self):
        """Set data_disk_usage_max_disk_size on the deployed cluster via the K8ssandraCluster CR.

        data_disk_usage_max_disk_size is a cassandra.yaml setting (default null/commented out,
        so the guardrail is inert unless set). It must be supplied through the operator-owned CR
        cassandraYaml block — NOT by editing cassandra.yaml inside the running pod, because the
        operator's config-builder re-renders cassandra.yaml from the CR on every reconcile /
        image swap, so a kubectl-exec append to the live file would be wiped on the very restart
        that triggers the crash. Patching the CR makes the setting persist across the buggy-image
        rolling restart, which is what actually runs guardrail validation at startup.

        This patch itself triggers a stock-image rolling restart; that restart boots fine because
        the data dir already exists (the data-dir deletion that arms the crash happens later, in
        setup_preconditions(), immediately before the buggy-image swap).
        """
        cluster = self.app.cluster_name
        ns = self.namespace
        logger.info(
            f"[AutoCassandra20787] Setting data_disk_usage_max_disk_size=1GiB on "
            f"K8ssandraCluster '{cluster}' in {ns} (cassandraYaml passthrough)"
        )
        # Cluster-level cassandraYaml passthrough (applies to all datacenters).
        patch = (
            '{"spec":{"cassandra":{"config":{"cassandraYaml":'
            '{"data_disk_usage_max_disk_size":"1GiB"}}}}}'
        )
        result = subprocess.run(
            f"kubectl patch k8ssandracluster {cluster} -n {ns} "
            f"--type=merge -p '{patch}'",
            shell=True, capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.warning(
                f"[AutoCassandra20787] set data_disk_usage_max_disk_size patch failed: "
                f"{result.stderr.strip()[:300]}"
            )
            return

        # The patch triggers a rolling restart of the datacenter; wait for it to settle so the
        # data-dir deletion in setup_preconditions() runs against a node that already has the
        # guardrail configured.
        subprocess.run(
            f"kubectl wait --for=condition=Ready k8ssandracluster/{cluster} "
            f"-n {ns} --timeout=600s",
            shell=True, capture_output=True, text=True,
        )
        logger.info(
            "[AutoCassandra20787] data_disk_usage_max_disk_size set and cluster Ready"
        )

    def setup_preconditions(self):
        """Delete the PVC-backed data directory on every Cassandra pod while the stock binary
        is still running, recreating the "fresh node" precondition the bug requires.

        The guardrail is already set via the CR (post_deploy). The stock nodes have already
        created /var/lib/cassandra/data on their PVCs, so the data dir is NOT absent the way a
        truly fresh node's would be. This deletes it immediately before the buggy-image swap
        (inject_buggy_image_expect_crash), so the buggy 5.0.4 node restarts over a PVC whose data
        dir is gone, runs guardrail validation before createAllDirectories(), and aborts with
        RuntimeException 'Cannot get data directories grouped by file store' caused by
        NoSuchFileException.

        Best-effort (see the module docstring's residual-risk note): if the management-api
        entrypoint pre-creates /var/lib/cassandra/data before launching Cassandra, this deletion
        would be undone before guardrail validation. The mount /var/lib/cassandra is left intact;
        only its `data` child (the configured data_file_directories[0]) is removed, matching the
        evidence-log precondition (`ls: cannot access '/var/lib/cassandra/data'` while
        /var/lib/cassandra is present).
        """
        pods = self._cassandra_pods()
        if not pods:
            logger.warning(
                "[AutoCassandra20787] No Cassandra pods found in namespace "
                f"{self.namespace!r} — cannot delete data dir to arm the crash"
            )
            return

        # Remove the PVC-backed data dir so the buggy node boots with it absent.
        delete_script = (
            f"rm -rf {_DATA_DIR}; "
            f"ls -ld {_DATA_DIR} 2>&1 || true"
        )

        for pod in pods:
            logger.info(
                f"[AutoCassandra20787] Deleting {_DATA_DIR} on pod {pod} (arming fresh-dir crash)"
            )
            cmd = (
                f"kubectl exec -n {self.namespace} {pod} -c cassandra -- "
                f"bash -c {subprocess.list2cmdline([delete_script])}"
            )
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode != 0:
                logger.warning(
                    f"[AutoCassandra20787] Deleting data dir on {pod} returned "
                    f"{result.returncode}: {result.stderr.strip()}"
                )
            else:
                logger.info(
                    f"[AutoCassandra20787] Data dir state on {pod}:\n{result.stdout.strip()}"
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
