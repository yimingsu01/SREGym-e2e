"""SASI tokenizer options not validated before being added to schema.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-15135

Title: SASI tokenizer options not validated before being added to schema.

Buggy: 4.0.0.  Fixed: 3.11.12, 4.0.1, 4.1-alpha1, 4.1.

Reproduction summary (config-gated, single node, startup failure):
  1. Enable SASI in cassandra.yaml (enable_sasi_indexes: true) — SASI is gated off
     by default in 4.0. Done by post_deploy() via the K8ssandraCluster cassandraYaml
     block so the operator performs a rolling restart and the gate stays open.
  2. CREATE a SASI CUSTOM INDEX with an illegal NonTokenizingAnalyzer option combo
     (case_sensitive='false' together with normalize_uppercase='true'). On buggy 4.0.0
     the index row is written to system_schema.indexes BEFORE the analyzer is
     instantiated; analyzer.init() then throws an uncaught IllegalArgumentException ->
     RuntimeException (client sees NoHostAvailable), but the bad index is now persisted.
  3. (Re)start the node. It re-loads the poisoned system_schema.indexes, hits the SAME
     IllegalArgumentException during startup, and FAILS TO BOOT (CrashLoopBackOff,
     exitCode 3). The framework's buggy-image swap supplies this final restart.
  The fix (IndexMode.java) calls analyzer.init(...) during validation and wraps the
  IllegalArgumentException in a ConfigurationException, rejecting the bad index BEFORE
  the schema write (fixed image returns a clean ConfigurationException and restarts fine).

Verbatim buggy signature (boot-failure, from the reproduction evidence log):
  Caused by: java.lang.IllegalArgumentException: case_sensitive option cannot be specified
  together with either normalize_lowercase or normalize_uppercase
    at org.apache.cassandra.index.sasi.analyzer.NonTokenizingOptions.buildFromMap(NonTokenizingOptions.java:110)
    at org.apache.cassandra.index.sasi.analyzer.NonTokenizingAnalyzer.init(NonTokenizingAnalyzer.java:61)
"""

import json
import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem

logger = logging.getLogger(__name__)


class AutoCassandra15135(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.0.0"
    source_git_ref = "cassandra-4.0.0"
    # Buggy version = fix patch (4.0.1) - 1. The bug already ships in the stock 4.0.0
    # image, so deploy/re-tag the stock image instead of an ~30-min ant-jar build.
    prebuilt_from_stock = True

    # IndexMode.validateOptions builds the SASI per-column index options and the analyzer
    # class, but in 4.0.0 it does not call analyzer.init(options) during validation, so an
    # illegal NonTokenizingAnalyzer option combination is not rejected before the index is
    # written to the schema. The fix adds the analyzer.init(...) call wrapped in a
    # ConfigurationException on this path.
    root_cause_file = "src/java/org/apache/cassandra/index/sasi/conf/IndexMode.java"
    root_cause_description = (
        "A SASI CUSTOM INDEX created with an illegal NonTokenizingAnalyzer option combination "
        "(case_sensitive='false' together with normalize_uppercase='true') is added to "
        "system_schema.indexes BEFORE its analyzer is instantiated. IndexMode does not call "
        "analyzer.init(options) during validateOptions in 4.0.0, so the illegal combination is "
        "not caught up front. The analyzer's init() (NonTokenizingAnalyzer.init -> "
        "NonTokenizingOptions.buildFromMap) then throws an uncaught IllegalArgumentException "
        "('case_sensitive option cannot be specified together with either normalize_lowercase "
        "or normalize_uppercase'), surfaced as a RuntimeException (client sees NoHostAvailable). "
        "Because the bad index row is already persisted, the node hits the SAME exception while "
        "loading system_schema.indexes on the next restart and fails to start (CrashLoopBackOff, "
        "exit code 3). The fix calls analyzer.init(...) during validation and wraps the "
        "IllegalArgumentException in a ConfigurationException so the index is rejected before "
        "the schema write."
    )

    # This is a config-gated STARTUP-CRASH bug, not a query-time bug. The crash_on_startup
    # branch in GenericCustomBuildProblem.inject_fault() runs setup_preconditions() (below)
    # while the (stock == buggy 4.0.0 code) binary is still up, then swaps in the buggy image
    # and waits for the node to enter CrashLoopBackOff — it never executes `reproducer` as CQL.
    # The string below documents the manual reproduction; the illegal CREATE that poisons the
    # schema is fired by setup_preconditions(), and the buggy-image swap provides the restart
    # that turns the poisoned schema into a boot failure.
    reproducer = """
-- CASSANDRA-15135 reproduction (config-gated startup failure).
-- Step 1 (cassandra.yaml, enabled by post_deploy via the operator):
--   enable_sasi_indexes: true        # SASI is gated off by default in 4.0
-- Step 2 (CQL, fired by setup_preconditions on the running node — poisons the schema):
CREATE KEYSPACE IF NOT EXISTS repro15135 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE IF NOT EXISTS repro15135.t (k int PRIMARY KEY, v text);
CREATE CUSTOM INDEX illegal_index ON repro15135.t(v)
  USING 'org.apache.cassandra.index.sasi.SASIIndex'
  WITH OPTIONS = {'mode':'CONTAINS',
                  'analyzer_class':'org.apache.cassandra.index.sasi.analyzer.NonTokenizingAnalyzer',
                  'case_sensitive':'false',
                  'normalize_uppercase':'true'};
-- On buggy 4.0.0 this CREATE returns NoHostAvailable (uncaught RuntimeException) but the
-- illegal_index row is ALREADY written to system_schema.indexes.
-- Step 3 (restart, supplied by the framework's buggy-image swap): the node re-loads the
-- poisoned system_schema.indexes and fails to start (CrashLoopBackOff, exit code 3) with
-- IllegalArgumentException at NonTokenizingOptions.buildFromMap(NonTokenizingOptions.java:110).
"""

    # Startup-crash bug: inject runs preconditions on the running binary, swaps to the buggy
    # image, and waits for CrashLoopBackOff rather than a Ready pod.
    crash_on_startup = True
    # Continuous so the mitigation oracle runs. No expected_output: this is a crash, so the
    # reproducer-pod probe checks readiness — NotReady/CrashLoopBackOff = bug present,
    # Ready = fixed (expect_unready=False).
    continuous_reproducer = True

    # The exact illegal CREATE that writes the unvalidated index into system_schema.indexes.
    _POISON_CQL = """
CREATE KEYSPACE IF NOT EXISTS repro15135 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE IF NOT EXISTS repro15135.t (k int PRIMARY KEY, v text);
CREATE CUSTOM INDEX illegal_index ON repro15135.t(v) USING 'org.apache.cassandra.index.sasi.SASIIndex' WITH OPTIONS = {'mode':'CONTAINS','analyzer_class':'org.apache.cassandra.index.sasi.analyzer.NonTokenizingAnalyzer','case_sensitive':'false','normalize_uppercase':'true'};
"""

    def post_deploy(self):
        """Enable SASI indexes on the deployed cluster before the reproducer runs.

        SASI is experimental and gated off by default in Cassandra 4.0
        (enable_sasi_indexes: false), so the illegal CREATE CUSTOM INDEX would be rejected
        for being a SASI index rather than for its illegal option combo, and the bug would
        never be reached. enable_sasi_indexes is a cassandra.yaml startup setting (not CQL-
        or runtime-settable), so we patch the operator-managed K8ssandraCluster CR's
        cassandraYaml block and let the operator perform a rolling restart so the gate opens
        and stays open. Patching the pod/StatefulSet directly would be reverted by the
        operator on the next reconcile.
        """
        cluster = self.app.cluster_name
        ns = self.app.namespace
        # JSON merge patch onto the dc1 datacenter's config.cassandraYaml (matches the
        # K8ssandraCluster manifest shape: spec.cassandra.datacenters[0].config).
        patch = json.dumps({
            "spec": {
                "cassandra": {
                    "datacenters": [
                        {
                            "metadata": {"name": "dc1"},
                            "config": {
                                "cassandraYaml": {
                                    "enable_sasi_indexes": True
                                }
                            },
                        }
                    ]
                }
            }
        })
        logger.info(
            f"[Cassandra15135] Enabling SASI on K8ssandraCluster '{cluster}' "
            f"(cassandraYaml.enable_sasi_indexes=true)"
        )
        subprocess.run(
            f"kubectl patch k8ssandracluster {cluster} -n {ns} --type=merge -p '{patch}'",
            shell=True, check=True, capture_output=True, text=True,
        )
        # Wait for the operator-driven rolling restart to bring the cluster back to Ready
        # before setup_preconditions runs the illegal CREATE CUSTOM INDEX.
        logger.info("[Cassandra15135] Waiting for cluster to be Ready after SASI rollout")
        self.app._wait_for_cluster_ready(timeout=600)
        logger.info("[Cassandra15135] SASI enabled; cluster Ready")

    def setup_preconditions(self):
        """Poison system_schema.indexes by running the illegal SASI CREATE.

        Runs while the (stock == buggy 4.0.0 code) binary is still up. The CREATE throws an
        uncaught RuntimeException (the client sees NoHostAvailable), but the unvalidated
        illegal_index row is written to system_schema.indexes — exactly the bug. The
        subsequent buggy-image swap (inject_buggy_image_expect_crash) restarts the node,
        which then fails to boot while re-loading the poisoned schema.
        """
        logger.info(
            "[Cassandra15135] Firing illegal SASI CREATE CUSTOM INDEX to poison "
            "system_schema.indexes (expect a server-side RuntimeException / NoHostAvailable)"
        )
        try:
            self.app.run_reproducer(self._POISON_CQL)
        except Exception as e:
            logger.info(
                f"[Cassandra15135] Illegal CREATE raised as expected (schema now poisoned): {e}"
            )
