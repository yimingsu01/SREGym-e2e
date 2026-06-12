"""SAI returns a missing row after partition delete + re-insert + flush.

Title: Correct the default behavior of compareTo() when comparing WIDE and STATIC PrimaryKeys.
JIRA:  https://issues.apache.org/jira/browse/CASSANDRA-20238
Component: Feature/SAI — Correctness / Unrecoverable Corruption / Loss (Critical).

Buggy: cassandra:5.0.3   Fixed: cassandra:5.0.4 (also 6.0-alpha1, 6.0).

Reproduction (single node, RF=1):
  Table with composite partition key ((pk0,pk1), ck0), a static column s1, value v0, and an
  SAI index on the PARTITION-KEY column pk0. Apply (pinned timestamps to fix the ordering):
  UPDATE (s1,v0 at ck0=0, ts=1000) -> partition DELETE (pk0,pk1, ts=2000) -> UPDATE creating
  the surviving row at ck0=1 (v0=1, ts=3000) -> nodetool flush (the defect is on the on-disk
  SAI path). Then `SELECT * WHERE v0=1 AND pk0=0 ALLOW FILTERING` must return that 1 row, but
  the buggy SAI path returns 0. A plain (non-SAI) read `WHERE pk0=0 AND pk1=1` proves the row
  physically exists on disk, so this is a wrong-result/missing-row bug, not data loss.

Verbatim buggy signature (from the reproduction evidence log):
  Buggy SAI result `(0 rows)` from
    `SELECT * FROM repro20238_ks.tbl WHERE v0=1 AND pk0=0 ALLOW FILTERING;`
  on 5.0.3, while the plain read
    `SELECT * FROM repro20238_ks.tbl WHERE pk0=0 AND pk1=1;`
  returns the row `0 | 1 | 1 | null | 1`.

Shape: wrong-result bug that requires a `nodetool flush` between the writes and the SELECT
(the SAI defect manifests only on the on-disk index path). The CQL-only continuous reproducer
pod cannot itself flush, so inject_fault() is overridden to do the writes, run the flush via
`kubectl exec`, fire the SAI SELECT, and then deploy a SELECT-only continuous reproducer that
keeps querying the already-flushed on-disk state. expected_output is the BUGGY value `(0 rows)`
(probe greps for it: Ready = bug present, NotReady = fixed).
"""

import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoCassandra20238(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "5.0.3"
    source_git_ref = "cassandra-5.0.3"
    # 5.0.3 already ships the bug (fix landed in 5.0.4), so deploy the stock image
    # instead of a ~30-min ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/index/sai/utils/PrimaryKey.java"
    root_cause_description = (
        "An SAI ALLOW FILTERING query returns a missing row after a partition is deleted and a "
        "row is re-inserted at a surviving clustering key, then flushed to disk. With a static "
        "column present and an SAI index on a partition-key column, `SELECT * WHERE v0=1 AND "
        "pk0=0 ALLOW FILTERING` returns (0 rows) on 5.0.3 even though a plain (non-SAI) read of "
        "the same partition returns the row, proving it is physically on disk. The root cause is "
        "in src/java/org/apache/cassandra/index/sai/utils/PrimaryKey.java: the default compareTo() "
        "behavior when comparing WIDE and STATIC PrimaryKeys is wrong, so on the on-disk SAI path "
        "the surviving wide row is incorrectly ordered/skipped relative to the static-row boundary "
        "left by the partition delete, and the matching row is filtered out of the result. Fixed "
        "in 5.0.4 (and 6.0-alpha1/6.0)."
    )

    # ── Reproducer pieces ──────────────────────────────────────────────────────
    # Pinned timestamps guarantee UPDATE(1000) < partition-DELETE(2000) < final UPDATE(3000),
    # so a timestamp collision can't delete the ck0=1 row "for the right reason". The static
    # column s1 is essential — removing it makes the bug vanish (per the reporter).
    _SETUP_CQL = """
CREATE KEYSPACE IF NOT EXISTS repro20238_ks WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE IF NOT EXISTS repro20238_ks.tbl (pk0 int, pk1 int, ck0 int, s1 int static, v0 int, PRIMARY KEY ((pk0, pk1), ck0));
CREATE INDEX IF NOT EXISTS tbl_pk0 ON repro20238_ks.tbl(pk0) USING 'sai';
UPDATE repro20238_ks.tbl USING TIMESTAMP 1000 SET s1=0, v0=0 WHERE pk0=0 AND pk1=1 AND ck0=0;
DELETE FROM repro20238_ks.tbl USING TIMESTAMP 2000 WHERE pk0=0 AND pk1=1;
UPDATE repro20238_ks.tbl USING TIMESTAMP 3000 SET v0=1 WHERE pk0=0 AND pk1=1 AND ck0=1;
"""

    # Fully-qualified so a SELECT-only run.cql needs no USE statement. On 5.0.3 (post-flush)
    # this returns (0 rows); on 5.0.4 it returns the 1 row.
    _SAI_SELECT = "SELECT * FROM repro20238_ks.tbl WHERE v0=1 AND pk0=0 ALLOW FILTERING;"

    # The documented end-to-end buggy path (writes -> flush -> SAI SELECT). The flush is not a
    # CQL statement, so it is annotated as a comment; inject_fault() performs it via kubectl exec.
    reproducer = (
        _SETUP_CQL
        + "-- nodetool flush repro20238_ks tbl   (run via kubectl exec in inject_fault; the SAI defect is on the on-disk path)\n"
        + _SAI_SELECT
        + "\n"
    )

    continuous_reproducer = True
    # Wrong-result bug: the buggy SAI query returns NO matching row, i.e. its output contains
    # the literal "(0 rows)". The mitigation probe greps for this buggy value, so the reproducer
    # pod is Ready while the bug is present and NotReady once it is fixed (the SAI query then
    # returns the row and "(0 rows)" no longer appears).
    expected_output = "(0 rows)"

    @mark_fault_injected
    def inject_fault(self):
        """Swap to the buggy image, stage writes, flush to disk, fire the SAI SELECT, then
        deploy a SELECT-only continuous reproducer.

        Fully overrides the base inject_fault(): the CQL-only continuous reproducer pod cannot
        issue `nodetool flush`, and the defect only manifests on the on-disk SAI path, so the
        flush must happen here exactly once. The continuous pod then keeps querying the
        already-flushed on-disk state with a SELECT-only workload (re-applying the writes in the
        loop would land the row in a fresh memtable, whose buggy read path may NOT miss the row,
        masking the bug).
        """
        if getattr(self, "_predeployed_buggy", False):
            logger.info("[AutoCassandra20238] Buggy image already deployed at cluster start — skipping swap")
        else:
            logger.info(f"[AutoCassandra20238] Injecting fault: swapping to buggy image {self._custom_image}")
            self.app.inject_buggy_image(self._custom_image)
            logger.info("[AutoCassandra20238] Buggy image active")

        logger.info("[AutoCassandra20238] Staging schema + pinned-timestamp writes")
        try:
            self.app.run_reproducer(self._SETUP_CQL)
        except Exception as e:
            logger.warning(f"[AutoCassandra20238] setup CQL raised: {e}")

        logger.info("[AutoCassandra20238] Flushing repro20238_ks.tbl to disk (on-disk SAI path)")
        self._nodetool_flush()

        logger.info("[AutoCassandra20238] Firing SAI SELECT to trigger the missing-row result")
        try:
            self.app.run_reproducer(self._SAI_SELECT)
        except Exception as e:
            logger.warning(f"[AutoCassandra20238] SAI SELECT raised: {e}")

        logger.info("[AutoCassandra20238] Deploying SELECT-only continuous reproducer")
        self.app.deploy_continuous_reproducer(self._SAI_SELECT, self.expected_output)

    def _nodetool_flush(self):
        """Run `nodetool flush repro20238_ks tbl` inside the Cassandra server pod.

        K8ssandra labels every managed pod with app.kubernetes.io/instance={cluster_name};
        the server container is named `cassandra`.
        """
        pod = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-l app.kubernetes.io/instance={self.app.cluster_name} "
            f"-o jsonpath='{{.items[0].metadata.name}}'",
            shell=True, capture_output=True, text=True,
        ).stdout.strip().strip("'")

        if not pod:
            logger.warning("[AutoCassandra20238] No Cassandra pod found — skipping nodetool flush")
            return

        result = subprocess.run(
            f"kubectl exec -n {self.namespace} {pod} -c cassandra -- "
            f"nodetool flush repro20238_ks tbl",
            shell=True, capture_output=True, text=True,
        )
        if result.returncode == 0:
            logger.info(f"[AutoCassandra20238] nodetool flush ok on pod {pod}")
        else:
            logger.warning(
                f"[AutoCassandra20238] nodetool flush exited {result.returncode}: "
                f"{result.stderr.strip()[:300]}"
            )
