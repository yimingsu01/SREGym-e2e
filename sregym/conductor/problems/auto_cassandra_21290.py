"""Implement atomic heartbeat file write.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-21290

Title: Implement atomic heartbeat file write.

Buggy: 4.1.11.  Fixed: 4.1.12 (unreleased), 5.0.8, 6.0, 6.0-alpha2
  (fix commit apache/cassandra@20d19c6627158415448312b50a2153310df42651, PR #4717).

Reproduction summary (config-gated, single node, startup failure):
  1. Enable the data_resurrection startup check in cassandra.yaml
     (startup_checks.check_data_resurrection.enabled: true, off by default),
     pointed at a PVC-backed heartbeat_file (/var/lib/cassandra/data/cassandra-heartbeat).
  2. Plant a 0-byte cassandra-heartbeat file at that path (the empty file a crash
     between create() and write() would leave behind — that race itself is not stageable).
  3. (Re)start the node. The check's execute() calls Heartbeat.deserializeFromJsonFile()
     on the empty/unparseable file; the buggy build throws StartupException
     (ERR_WRONG_DISK_STATE) with no fallback and aborts startup (exitCode=3, CrashLoopBackOff).
  The fix writes the heartbeat file atomically (temp file + atomic rename, so a crash can no
  longer leave it empty) AND on read falls back to the file's last-modified time when it
  cannot be parsed, instead of failing startup.

Verbatim buggy signature (from the reproduction evidence log; last log line before the
JVM exits, on every boot attempt):
  ERROR [main] CassandraDaemon.java:900 - Failed to deserialize heartbeat file /var/lib/cassandra/data/cassandra-heartbeat
  (followed by JVM exit code 3 / pod CrashLoopBackOff)

Caveat (from the evidence log): check_data_resurrection is OFF by default, so only
deployments that explicitly enable it are exposed to this bug; post_deploy() enables it.
The empty-file ARTIFACT is staged directly — the crash race that produces it in the wild is
the un-stageable part and is not raced.

Runtime-fidelity note: the evidence log reproduced this on a single bare pod with an emptyDir
and a pod `command` override. The SREGym runtime instead deploys a 3-node K8ssandra-operator
cluster, so the cassandra.yaml enable is applied via the operator-owned CR (post_deploy) and
the empty file is planted on the data PVC (setup_preconditions) so both survive the
buggy-image rolling restart. The bug itself is single-node by nature (a per-node startup-check
parse failure); the 3-node cluster is incidental infrastructure.

Sequence note: in the log the 0-byte file existed at first boot; here post_deploy enables the
check, the stock node boots with the file ABSENT (per Control A the enabled check then creates
a VALID heartbeat file), and setup_preconditions truncates it to 0 bytes immediately before the
buggy-image swap. Empty (not absent) is the trigger, so the truncate is what arms the crash; it
runs right before the swap to minimize any window in which the stock node could refresh it.
"""

import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem

logger = logging.getLogger(__name__)

# DataResurrectionCheck.DEFAULT_HEARTBEAT_FILE resolves to
# DatabaseDescriptor.getLocalSystemKeyspacesDataFileLocations()[0]/cassandra-heartbeat,
# i.e. /var/lib/cassandra/data/cassandra-heartbeat (per the evidence log). This path is
# under the K8ssandra data PVC mount (/var/lib/cassandra), so a 0-byte file planted here
# survives the operator's rolling restart and is the file the restarted node reads.
_HEARTBEAT_FILE = "/var/lib/cassandra/data/cassandra-heartbeat"
_HEARTBEAT_DIR = "/var/lib/cassandra/data"


class AutoCassandra21290(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.1.11"
    source_git_ref = "cassandra-4.1.11"
    # 4.1.11 already ships the bug (fix landed in 4.1.12, which is unreleased — the 4.1
    # Docker ceiling is 4.1.11), so deploy/re-tag the stock image instead of an ~30-min
    # ant-jar build.
    prebuilt_from_stock = True

    # The check_data_resurrection StartupCheck's heartbeat read/write path. execute() calls
    # Heartbeat.deserializeFromJsonFile(heartbeatFile); on parse failure the buggy 4.1.11
    # build throws StartupException(ERR_WRONG_DISK_STATE, "Failed to deserialize heartbeat
    # file " + heartbeatFile). The fix (commit 20d19c6...) makes the WRITE atomic
    # (FBUtilities.serializeToJsonFileAtomic, temp file + atomic rename) and on READ falls
    # back to the file's last-modified time instead of aborting.
    root_cause_file = "src/java/org/apache/cassandra/service/DataResurrectionCheck.java"
    root_cause_description = (
        "With the data_resurrection startup check enabled "
        "(startup_checks.check_data_resurrection.enabled=true), an empty (0-byte) "
        "cassandra-heartbeat file prevents the node from starting. The check is implemented "
        "in DataResurrectionCheck (inner class Heartbeat); its execute() calls "
        "Heartbeat.deserializeFromJsonFile(heartbeatFile), and on an unparseable/empty file "
        "the buggy 4.1.11 path throws StartupException(ERR_WRONG_DISK_STATE, 'Failed to "
        "deserialize heartbeat file ...') with no fallback, aborting startup with exit "
        "code 3 (CrashLoopBackOff). The empty file is what a crash between the heartbeat "
        "file's create() and write() would leave behind. The fix (4.1.12/5.0.8/6.0) writes "
        "the heartbeat file atomically (write to a temp file then atomic-rename, so a crash "
        "can no longer leave it empty) and, on read, falls back to the file's last-modified "
        "time when the contents cannot be parsed instead of failing startup."
    )

    # This is a config-gated STARTUP-CRASH bug, not a query-time bug. The crash_on_startup
    # branch in GenericCustomBuildProblem.inject_fault() runs setup_preconditions() (below)
    # while the stock binary is still up, then swaps in the buggy image and waits for the
    # node to enter CrashLoopBackOff — it never executes `reproducer` as CQL. The string
    # below therefore documents the manual reproduction steps rather than runnable CQL.
    reproducer = """
-- CASSANDRA-21290 reproduction (config-gated startup failure; NOT executable CQL).
-- The data_resurrection startup check is enabled via the K8ssandraCluster CR (post_deploy),
-- the 0-byte heartbeat file is planted on the data PVC (setup_preconditions), then the
-- buggy-image swap restarts the node, which trips on the empty/unparseable heartbeat file
-- during the startup check.
--
-- 1. Enable the data_resurrection startup check in cassandra.yaml (off by default):
--      startup_checks:
--        check_data_resurrection:
--          enabled: true
--          heartbeat_file: /var/lib/cassandra/data/cassandra-heartbeat
-- 2. Plant a 0-byte cassandra-heartbeat file at that path (the empty file a crash between
--    create() and write() would leave behind):
--      mkdir -p /var/lib/cassandra/data
--      : > /var/lib/cassandra/data/cassandra-heartbeat   # 0 bytes
-- 3. (Re)start the node. The startup check's execute() calls
--    Heartbeat.deserializeFromJsonFile() on the empty file and the buggy build throws
--    StartupException(ERR_WRONG_DISK_STATE) -> "Failed to deserialize heartbeat file
--    /var/lib/cassandra/data/cassandra-heartbeat"; the node never reaches CQL (exitCode=3).
--
-- A/B control (within-version, same 4.1.11 image, per the evidence log): an ABSENT heartbeat
-- file (check enabled, normal first boot) is TOLERATED — the check creates it with valid JSON
-- and the node boots. Only the EMPTY-file state crashes startup, isolating content emptiness
-- as the sole trigger.
"""

    # Startup-crash bug: inject runs preconditions on the stock binary, swaps to the buggy
    # image, and waits for CrashLoopBackOff rather than a Ready pod.
    crash_on_startup = True
    # Diagnosis-only. The crash_on_startup branch of inject_fault() returns before
    # deploy_continuous_reproducer(), so the {cluster_name}-reproducer Deployment that
    # ReproducerPodMitigationOracle inspects is never created — a mitigation oracle would
    # hit its 404 branch (success=False) for BOTH the buggy and the fixed build and could
    # not discriminate. Leaving continuous_reproducer False makes mitigation_oracle = None
    # (diagnosis graded by LLMAsAJudgeOracle on the root cause), matching the
    # auto_cassandra_17933 / etcd / tidb crash_on_startup precedents. No expected_output
    # (this is a crash, not a wrong result).
    continuous_reproducer = False

    def post_deploy(self):
        """Enable the data_resurrection startup check on the deployed cluster via the
        K8ssandraCluster CR.

        startup_checks.check_data_resurrection.enabled is a cassandra.yaml STARTUP setting,
        disabled by default in 4.1.x. It must be enabled through the operator-owned CR
        cassandraYaml block — NOT by editing cassandra.yaml inside the running pod, because
        the operator's config-builder re-renders cassandra.yaml from the CR on every reconcile
        / image swap, so a kubectl-exec append to the live file would be wiped on the very
        restart that triggers the crash. Patching the CR makes the check persist across the
        buggy-image rolling restart, which is what actually runs the heartbeat deserialize at
        startup and trips on the empty file.

        heartbeat_file points at a PVC-backed path (/var/lib/cassandra/data/cassandra-heartbeat)
        so the 0-byte file planted by setup_preconditions() is the file the restarted node reads.
        """
        cluster = self.app.cluster_name
        ns = self.namespace
        logger.info(
            f"[AutoCassandra21290] Enabling data_resurrection startup check on "
            f"K8ssandraCluster '{cluster}' in {ns} "
            f"(cassandraYaml.startup_checks.check_data_resurrection.enabled=true, "
            f"heartbeat_file={_HEARTBEAT_FILE})"
        )
        # Cluster-level cassandraYaml passthrough (applies to all datacenters). startup_checks
        # is a structured cassandra.yaml key, so it is supplied as a nested object.
        patch = (
            '{"spec":{"cassandra":{"config":{"cassandraYaml":'
            '{"startup_checks":{"check_data_resurrection":{"enabled":true,'
            f'"heartbeat_file":"{_HEARTBEAT_FILE}"' + '}}}}}}}'
        )
        result = subprocess.run(
            f"kubectl patch k8ssandracluster {cluster} -n {ns} "
            f"--type=merge -p '{patch}'",
            shell=True, capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.warning(
                f"[AutoCassandra21290] enable check_data_resurrection patch failed: "
                f"{result.stderr.strip()[:300]}"
            )
            return

        # The patch triggers a rolling restart of the datacenter; wait for it to settle so
        # setup_preconditions() plants the file against a node that already has the check
        # configured (the file goes onto the PVC, surviving the later buggy-image restart).
        subprocess.run(
            f"kubectl wait --for=condition=Ready k8ssandracluster/{cluster} "
            f"-n {ns} --timeout=600s",
            shell=True, capture_output=True, text=True,
        )
        logger.info("[AutoCassandra21290] data_resurrection check enabled and cluster Ready")

    def setup_preconditions(self):
        """Plant a 0-byte cassandra-heartbeat file in every Cassandra pod's PVC-backed data
        directory while the stock binary is still running.

        The data_resurrection check is already enabled via the CR (post_deploy). The
        subsequent buggy-image swap (inject_buggy_image_expect_crash) restarts the node, whose
        startup check then calls Heartbeat.deserializeFromJsonFile() on this empty file and
        aborts with StartupException(ERR_WRONG_DISK_STATE). The file lives under
        /var/lib/cassandra (the data PVC), so it persists across the restart.

        Note (from the evidence log): an ABSENT heartbeat file is tolerated (the check creates
        a valid one and the node boots), so the trigger is specifically the EMPTY-file state —
        the file is planted with normal mode and 0 bytes, and the failure is a deserialize/parse
        error, not a permission/IO error.
        """
        pods = self._cassandra_pods()
        if not pods:
            logger.warning(
                "[AutoCassandra21290] No Cassandra pods found in namespace "
                f"{self.namespace!r} — cannot plant 0-byte heartbeat file"
            )
            return

        # Create the data dir and plant the 0-byte heartbeat file on the PVC.
        plant_script = (
            f"mkdir -p {_HEARTBEAT_DIR}; "
            f": > {_HEARTBEAT_FILE}; "
            f"ls -l {_HEARTBEAT_FILE}"
        )

        for pod in pods:
            logger.info(
                f"[AutoCassandra21290] Planting 0-byte {_HEARTBEAT_FILE} on pod {pod}"
            )
            cmd = (
                f"kubectl exec -n {self.namespace} {pod} -c cassandra -- "
                f"bash -c {subprocess.list2cmdline([plant_script])}"
            )
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode != 0:
                logger.warning(
                    f"[AutoCassandra21290] Planting file on {pod} returned "
                    f"{result.returncode}: {result.stderr.strip()}"
                )
            else:
                logger.info(
                    f"[AutoCassandra21290] Heartbeat file on {pod}:\n{result.stdout.strip()}"
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
