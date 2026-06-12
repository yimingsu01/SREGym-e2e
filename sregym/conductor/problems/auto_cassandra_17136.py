"""CASSANDRA-17136: Enabling Full Query Logging via nodetool can trip disk_failure_policy and offline the node.

Title: FQL: Enabling via nodetool can trigger disk_failure_mode
JIRA:  https://issues.apache.org/jira/browse/CASSANDRA-17136

Buggy: cassandra 4.0.1   Fixed: cassandra 4.0.2

Reproduction (single node, default disk_failure_policy=stop):
  1. Under the FQL --path location, stage a NON-EMPTY subdirectory that the Cassandra
     process user (uid cassandra) is allowed to list but NOT allowed to delete from
     (parent dir 777 so nodetool's rwx-of-parent check passes; inner subdir root-owned
     mode 555 containing a file).
  2. Run `nodetool enablefullquerylog --path <dir>`. FQL's BinLog.cleanDirectory recurses
     into the staged subdir, hits the undeletable file, and throws AccessDeniedException.
  3. That FS error is routed through the disk_failure_policy=stop handler, which stops
     native transport + gossip and OFFLINES the node (cqlsh -> ConnectionRefused).

The fixed image (4.0.2) runs the identical workload with no offlining.

Verbatim buggy signature (the undesirable offlining — from `kubectl logs`):
  ERROR [RMI TCP Connection(8)-127.0.0.1] DefaultFSErrorHandler.java:64 - Stopping transports as disk_failure_policy is stop

Client-visible signature when enabling FQL (the AccessDeniedException):
  java.nio.file.AccessDeniedException: /trap/dir/file
    at org.apache.cassandra.io.util.FileUtils.deleteWithConfirm(FileUtils.java:250)
    at org.apache.cassandra.utils.binlog.BinLog.deleteRecursively(BinLog.java:492)
    at org.apache.cassandra.utils.binlog.BinLog.cleanDirectory(BinLog.java:477)
    at org.apache.cassandra.utils.binlog.BinLog$Builder.build(BinLog.java:436)
    at org.apache.cassandra.fql.FullQueryLogger.enable(FullQueryLogger.java:106)
    at org.apache.cassandra.service.StorageService.enableFullQueryLogger(StorageService.java:5915)

Shape: nodetool sequence (not pure CQL). The trigger is a filesystem trap + a `nodetool`
command, so inject_fault() is overridden to kubectl-exec the trap setup and the nodetool
call (the 20108 pattern) rather than running CQL via cqlsh.

continuous_reproducer is intentionally False (diagnosis-only). The shared Cassandra
DBBuildSpec deploys a 3-node DC, but this bug offlines only the SINGLE node nodetool
targets; the continuous-reproducer probe runs cqlsh against the load-balanced
{cluster}-dc1-service, which routes around the dead node and stays Ready — i.e. it would
report "fixed" while the bug is fully present. A correct mitigation probe would have to
target the specific offlined pod's `nodetool statusbinary`, which needs custom manifest
infrastructure beyond a CQL reproducer string. A diagnosis-only oracle (matching the
cassandra_20108 precedent) is correct here; a wrong mitigation oracle would be worse.
"""

import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoCassandra17136(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.0.1"
    source_git_ref = "cassandra-4.0.1"
    # 4.0.1 already ships the bug, so deploy the stock image instead of an ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/utils/binlog/BinLog.java"
    root_cause_description = (
        "Enabling Full Query Logging via `nodetool enablefullquerylog --path <dir>` cleans the "
        "target directory: BinLog.cleanDirectory() recursively deletes the directory contents "
        "(BinLog.deleteRecursively -> FileUtils.deleteWithConfirm). If any file under that path "
        "cannot be deleted by the Cassandra process user, a java.nio.file.AccessDeniedException "
        "is raised. That exception is treated as a generic filesystem error and routed through "
        "the disk_failure_policy handler (default `stop`), which stops native transport and "
        "gossip and offlines the node. A failure to clean a user-supplied FQL directory should "
        "not be handled as a disk failure that takes the node offline."
    )

    # Filesystem trap + nodetool steps that trigger the bug. Encoded for documentation/oracle
    # context; inject_fault() below executes these via kubectl exec (NOT as CQL through cqlsh,
    # which is what the default run_reproducer would do).
    reproducer = """
# Stage a non-empty, undeletable subdirectory under the FQL --path location.
# Parent /trap is 777 so nodetool's "parent is rwx for the server user" check passes;
# the inner /trap/dir is root-owned mode 555, so the cassandra user can LIST it (and find
# /trap/dir/file) but cannot DELETE the file inside it.
mkdir -p /trap/dir
touch /trap/dir/file
chmod 777 /trap
chmod 555 /trap/dir

# Enable FQL over the trap. BinLog.cleanDirectory recurses into /trap/dir, fails to delete
# /trap/dir/file with AccessDeniedException, which trips disk_failure_policy=stop and offlines
# the node (native transport + gossip stopped; subsequent cqlsh -> Connection refused).
nodetool enablefullquerylog --path /trap
"""
    # Diagnosis-only: see the module docstring — the 3-node DC service routes around the single
    # offlined node, so the default cqlsh readiness probe cannot detect this bug.
    continuous_reproducer = False

    @mark_fault_injected
    def inject_fault(self):
        """Swap to the buggy image, then trigger the FQL/disk_failure_policy bug.

        This bug is a `nodetool` + filesystem-trap sequence, not a CQL query, so we override
        inject_fault() (rather than relying on the base class running `reproducer` through
        cqlsh). We resolve ONE Cassandra pod and reuse it for both steps: the trap lives on
        that pod's filesystem and `nodetool enablefullquerylog` targets that pod's local JVM,
        so they must hit the same node.
        """
        if self._predeployed_buggy:
            logger.info("[Cassandra17136] Buggy image already deployed at cluster start — skipping swap")
        else:
            logger.info(f"[Cassandra17136] Swapping cluster to buggy image: {self._custom_image}")
            self.app.inject_buggy_image(self._custom_image)
            logger.info("[Cassandra17136] Buggy image active")

        pod = self._first_cassandra_pod()
        if not pod:
            logger.warning("[Cassandra17136] No Cassandra pod found — cannot inject fault")
            return

        # Step 1: stage the undeletable, non-empty subdirectory under the FQL --path.
        # kubectl exec runs as the container's default user (root in the reference repro),
        # which is what lets it create a directory the cassandra JVM user cannot delete from.
        trap_cmd = (
            "mkdir -p /trap/dir && touch /trap/dir/file && "
            "chmod 777 /trap && chmod 555 /trap/dir && ls -la /trap /trap/dir"
        )
        logger.info(f"[Cassandra17136] Staging FQL trap on pod {pod}")
        self._exec_in_pod(pod, trap_cmd)

        # Step 2: enable FQL over the trap. nodetool exits non-zero with
        # `error: /trap/dir/file` (AccessDeniedException) — that is EXPECTED, not a failure.
        # Server-side, this trips disk_failure_policy=stop and offlines the node.
        logger.info(f"[Cassandra17136] Enabling full query log over trap on pod {pod} (offlines the node)")
        self._exec_in_pod(pod, "nodetool enablefullquerylog --path /trap")
        logger.info("[Cassandra17136] FQL enable issued — node expected to be offlined by disk_failure_policy=stop")

    @mark_fault_injected
    def recover_fault(self):
        """Restore the stock image and wait for the cluster to be Ready."""
        logger.info("[Cassandra17136] Recovering: restoring cluster to stock image")
        self.app.restore_stock_image(custom_image=self._custom_image)
        logger.info("[Cassandra17136] Recovery complete")

    # ── helpers ────────────────────────────────────────────────────────────────

    def _first_cassandra_pod(self) -> str:
        """Return the name of one Cassandra pod for this cluster (or "" if none)."""
        out = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-l app.kubernetes.io/instance={self.app.cluster_name} "
            f"--no-headers -o custom-columns=NAME:.metadata.name",
            shell=True, capture_output=True, text=True,
        ).stdout
        pods = [p.strip() for p in out.splitlines() if p.strip()]
        return pods[0] if pods else ""

    def _exec_in_pod(self, pod: str, command: str) -> None:
        """Run a shell command in the cassandra container of `pod`. nodetool returning
        non-zero (AccessDeniedException) is expected for the trigger, so failures are logged
        rather than raised."""
        result = subprocess.run(
            f"kubectl exec -n {self.namespace} {pod} -c cassandra -- bash -c {self._shquote(command)}",
            shell=True, capture_output=True, text=True,
        )
        combined = (result.stdout + result.stderr).strip()
        logger.info(f"[Cassandra17136] exec rc={result.returncode}: {combined[:400]}")

    @staticmethod
    def _shquote(s: str) -> str:
        """Single-quote a string for safe embedding in a shell command line."""
        return "'" + s.replace("'", "'\\''") + "'"
