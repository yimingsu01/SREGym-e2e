"""CASSANDRA-21348: system_views.settings ClassCastException for enum-keyed settings.

Title: SELECT * FROM system_views.settings throws ClassCastException when a
       startup check is enabled (the startup_checks setting becomes an
       enum-keyed map whose value cannot be rendered as a String).

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-21348

Buggy: 5.0.8 (config-gated). The bug already ships in the released 5.0.8 image —
there is no version bump for the fix, so we deploy stock 5.0.8 and inject the
fault by enabling a startup check in cassandra.yaml.

Reproduction (config-gated, single-node, query-time error — NOT a startup crash):
  1. Enable a startup check in cassandra.yaml so the `startup_checks` setting
     becomes an enum-keyed (StartupChecks$StartupCheckType) map:
         startup_checks:
           check_data_resurrection:
             enabled: true
  2. Cassandra boots cleanly. Then run:  SELECT * FROM system_views.settings;
  3. The SettingsTable virtual table tries to render the non-String enum-keyed
     map value as a String and throws.
  Control: stock 5.0.8 with NO startup_checks config returns the rows cleanly,
  isolating the fault to the enum-keyed setting value (not the table itself).

Verbatim buggy signature:
  ClassCastException: org.apache.cassandra.service.StartupChecks$StartupCheckType cannot be cast to java.lang.String
"""

import json
import logging
import subprocess
import time

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem

logger = logging.getLogger(__name__)


class AutoCassandra21348(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "5.0.8"
    source_git_ref = "cassandra-5.0.8"
    # The buggy code already ships in the released 5.0.8 image; the "fault" is a
    # config behavior, not a code patch. Deploy the stock image instead of an
    # ~30-min ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/db/virtual/SettingsTable.java"
    root_cause_description = (
        "SELECT * FROM system_views.settings throws "
        "'ClassCastException: org.apache.cassandra.service.StartupChecks$StartupCheckType "
        "cannot be cast to java.lang.String' once a startup check is enabled. Enabling a "
        "startup check turns the `startup_checks` setting into an enum-keyed "
        "(StartupChecks$StartupCheckType) map. The 5.0 system_views.settings virtual table "
        "(SettingsTable.java) casts every setting value to String when materializing rows, "
        "but the enum-keyed map value is not a String, so the cast fails."
    )

    # The reproducer is ONLY the CQL — the continuous-reproducer pod feeds this
    # string to cqlsh, so the YAML config must NOT be here. The startup_checks
    # config is applied in setup_preconditions() at fault-injection time, while
    # the (stock, already-buggy) cluster is healthy.
    reproducer = """
SELECT * FROM system_views.settings;
"""
    continuous_reproducer = True
    # This bug ERRORS (ClassCastException); it does not return a wrong value, so
    # no expected_output. The mitigation oracle uses expect_unready=False:
    # NotReady = bug present (query keeps erroring), Ready = fixed.

    def setup_preconditions(self):
        """Enable a startup check in cassandra.yaml so `startup_checks` becomes an
        enum-keyed map (the precondition that exposes the SettingsTable cast bug).

        Runs during inject_fault() while the cluster is healthy. We patch the
        K8ssandraCluster CR's spec.cassandra.config.cassandraYaml field; the
        cass-operator merges it into cassandra.yaml and performs a rolling
        restart. We then wait for the cluster to be Ready again so the SELECT in
        inject_fault()/the reproducer pod hits the new config and the bug fires.

        NOTE: the config is applied via the CR `cassandraYaml` passthrough. If a
        future operator schema rejects the `startup_checks` key, this patch path
        may need adjustment.
        """
        patch = {
            "spec": {
                "cassandra": {
                    "config": {
                        "cassandraYaml": {
                            "startup_checks": {
                                "check_data_resurrection": {"enabled": True}
                            }
                        }
                    }
                }
            }
        }
        patch_json = json.dumps(patch)
        cmd = (
            f"kubectl patch {self.app.spec.cr_kind} {self.app.cluster_name} "
            f"-n {self.namespace} --type=merge -p '{patch_json}'"
        )
        logger.info(
            "[AutoCassandra21348] Enabling startup_checks.check_data_resurrection "
            "in cassandra.yaml via K8ssandraCluster CR"
        )
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                logger.warning(
                    f"[AutoCassandra21348] cassandraYaml patch failed: {result.stderr.strip()}"
                )
            # Give the operator time to detect the spec change and begin the
            # rolling restart, then wait for the cluster to be Ready again so the
            # new cassandra.yaml (with the enum-keyed startup_checks map) is live
            # before the reproducer SELECT runs.
            time.sleep(20)
            self.app._wait_for_cluster_ready()
        except Exception as e:
            logger.warning(f"[AutoCassandra21348] setup_preconditions raised: {e}")
