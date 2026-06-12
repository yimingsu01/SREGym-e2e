"""CASSANDRA-14496: TWCS disables tombstone compactions when only unchecked_tombstone_compaction is set.

Title: TimeWindowCompactionStrategy (TWCS) erroneously disables tombstone compactions
       when a table sets unchecked_tombstone_compaction=true but sets NEITHER
       tombstone_threshold NOR tombstone_compaction_interval.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-14496

Buggy: 4.0.0   Fixed: 4.0.1 (fix persists through the 4.0 line; also 3.11.11, 4.1).

Reproduction (single-node, pure CQL — identical statements reproduce on the buggy build):
  1. CREATE KEYSPACE repro14496_ks (SimpleStrategy, RF=1).
  2. CREATE TABLE ... WITH compaction = {'class':'TimeWindowCompactionStrategy', ...,
     'unchecked_tombstone_compaction':'true'} and NO tombstone_threshold / NO
     tombstone_compaction_interval.
  On buggy 4.0.0 the TWCS constructor's containsKey check only inspects
  tombstone_compaction_interval and tombstone_threshold and ignores
  unchecked_tombstone_compaction, so it sets disableTombstoneCompactions=true and logs
  "Disabling tombstone compactions for TWCS" at TWCS instantiation (CREATE TABLE,
  MigrationStage). The fixed build logs "Enabling" for the byte-for-byte identical table.

Verbatim buggy signature (cass-bug, cassandra:4.0.0, /var/log/cassandra/debug.log):
  DEBUG [MigrationStage:1] 2026-06-12 04:24:44,927 TimeWindowCompactionStrategy.java:65 - Disabling tombstone compactions for TWCS

Observability caveat (see the evidence log "Note on observability"): the client/operator-visible
signature is a DEBUG-level log line that lands in /var/log/cassandra/debug.log (NOT in `kubectl logs`
/ system.log, which are INFO-filtered, and NOT in cqlsh stdout). The CREATE TABLE itself SUCCEEDS on
both buggy and fixed builds (no CQL error, no wrong query result). Consequently this is neither a
wrong-result bug (so expected_output is intentionally left unset) nor a CQL-error bug — the standard
ReproducerPodMitigationOracle, which only inspects cqlsh stdout/exit code, cannot discriminate buggy
from fixed for this bug. The meaningful oracle here is the diagnosis LLMAsAJudgeOracle on the root cause.
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra14496(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.0.0"
    source_git_ref = "cassandra-4.0.0"
    # 4.0.0 already ships the bug (fix landed in 4.0.1), so deploy the stock image
    # instead of running a ~30-min ant-jar source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/db/compaction/TimeWindowCompactionStrategy.java"
    root_cause_description = (
        "A TWCS table created with unchecked_tombstone_compaction=true but with NEITHER "
        "tombstone_threshold NOR tombstone_compaction_interval set has tombstone compactions "
        "silently DISABLED on buggy 4.0.0 — the opposite of what unchecked_tombstone_compaction=true "
        "is meant to do. In TimeWindowCompactionStrategy.java the constructor only checks "
        "containsKey(TOMBSTONE_COMPACTION_INTERVAL_OPTION) and containsKey(TOMBSTONE_THRESHOLD_OPTION); "
        "it ignores unchecked_tombstone_compaction, so the table falls into the "
        "disableTombstoneCompactions=true branch and logs 'Disabling tombstone compactions for TWCS'. "
        "The fix makes the constructor honor unchecked_tombstone_compaction (logging 'Enabling' instead)."
    )

    # Single-node, pure-CQL reproducer copied verbatim from the evidence log's
    # "Reproducer" section. Run on the buggy 4.0.0 build it instantiates TWCS with
    # tombstone compactions disabled (DEBUG "Disabling tombstone compactions for TWCS").
    reproducer = """
CREATE KEYSPACE IF NOT EXISTS repro14496_ks WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE repro14496_ks.twcs2 (
  id text, ts timestamp, val text, PRIMARY KEY (id, ts)
) WITH compaction = {
  'class':'TimeWindowCompactionStrategy',
  'compaction_window_unit':'DAYS',
  'compaction_window_size':'1',
  'unchecked_tombstone_compaction':'true'
};
"""
    continuous_reproducer = True
    # NOTE: expected_output is intentionally NOT set. This is not a wrong-result bug
    # (the CREATE succeeds and no query returns a wrong value); the buggy signature is a
    # DEBUG log line in debug.log that the cqlsh-stdout-based mitigation probe cannot see.
