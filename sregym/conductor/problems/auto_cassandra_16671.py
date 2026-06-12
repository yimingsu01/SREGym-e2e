"""CASSANDRA-16671: Cassandra can return no row when the row columns have been deleted.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16671

Buggy: 3.11.10  ->  Fixed: 3.11.11 (fix commit 24346d17899df8610a5f425c7074ddd5dc8082bb).
Component: Legacy/Local Write-Read Paths.

STUB: multi-node reproduction not yet encoded as a single-cluster Problem — see steps below.

Reproduction summary (2-node ring, RF=2 SimpleStrategy, local read-path bug):
  On a two-node ring with RF=2, per-replica divergence is injected via gossip isolation
  (clean bidirectional DN) so each CONSISTENCY ONE write lands on exactly one replica,
  with hinted handoff disabled so the divergence is preserved. cass-0 gets
  INSERT(pk=1,ck='1',v=1) USING TIMESTAMP 1000 -> flush -> UPDATE SET v=2 USING TIMESTAMP
  2000 -> flush (TWO sstables: the INSERT sstable carries PK liveness; the all-columns
  UPDATE sstable carries NO row liveness). cass-1 gets DELETE v USING TIMESTAMP 3000 ->
  flush (a column tombstone only). After gossip is re-enabled (both UN), a read coordinated
  from cass-1 at `CONSISTENCY ALL; SELECT * FROM ks16671.tbl WHERE pk=1 AND ck='1'` must
  return `row(1, '1', null)` per CQL semantics (a row exists while it has one non-null
  column, incl. PK columns). The buggy 3.11.10 WRONGLY returns (0 rows): cass-0's local
  timestamp-ordered read stops early on the all-columns UPDATE sstable, dropping the row's
  PK liveness; merged with cass-1's column deletion the coordinator then drops the whole row.

This bug is a per-replica-divergence + local-read-path regression (from CASSANDRA-16226);
it CANNOT manifest on a single node and CANNOT be expressed as one single-cluster CQL
string. It requires multi-pod orchestration (per-node gossip isolation + per-node CL ONE
writes + per-node flush so cass-0 holds two divergent sstables and cass-1 holds a column
tombstone) that is not yet available in the single-cluster GenericCustomBuildProblem harness.
The full reproducer steps from the evidence log are preserved in the `reproducer` field below
so this can be promoted to a real multi-node Problem later. See the authoritative evidence log:
.claude/repro-evidence/repro-CASSANDRA-16671.md

Verbatim buggy signature (cassandra:3.11.10, coordinator cass-1, CL=ALL, FIRST read):
  Consistency level set to ALL.

   pk | ck | v
  ----+----+---


  (0 rows)

Contrast on the SAME buggy node — `SELECT v` returns 1 row (null), proving the row data is
present and only `SELECT *` regresses:
  Consistency level set to ALL.

   v
  ------
   null

  (1 rows)

Fixed 3.11.11 returns, for the identical workload, `SELECT *` => `1 | 1 | null` (1 rows).
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra16671(GenericCustomBuildProblem):
    # STUB: see module docstring. Multi-node (2-node ring, RF=2) local-read-path bug that
    # only manifests with per-replica divergence and CANNOT be reproduced by a single-cluster
    # CQL reproducer (the standard deploy gives one cluster and one merged dataset). Fields
    # below are set so the problem registers and carries the root-cause + full multi-node
    # steps, but the `reproducer` is NOT a runnable single-cluster CQL block (no
    # continuous_reproducer, no expected_output — deliberately, to avoid a false "it works"
    # mitigation oracle that would loop the SELECT against the single cluster, where the
    # per-replica divergence does not exist, and report the bug "reproduced/mitigated").
    db_name = "cassandra"
    db_version = "3.11.10"
    source_git_ref = "cassandra-3.11.10"
    # 3.11.10 already ships the bug (buggy = fix patch 3.11.11 - 1), so deploy the stock image
    # instead of running a ~30-min `ant jar` source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/db/SinglePartitionReadCommand.java"
    root_cause_description = (
        "Cassandra wrongly returns no row (0 rows) for a `SELECT *` when a row's non-PK "
        "columns have been deleted on one replica while another replica holds the row via an "
        "all-columns UPDATE. This is a local read-path regression introduced by CASSANDRA-16226 "
        "in SinglePartitionReadCommand.java: the timestamp-ordered read (isRowComplete / the "
        "queryMemtableAndDiskInternal early-stop) stops EARLY when an UPDATE covering all "
        "requested columns is found in an SSTable. CQL semantics say a row exists as long as it "
        "has one non-null column INCLUDING primary-key columns: INSERT sets the row's "
        "primary-key liveness, UPDATE does NOT. cass-0's two sstables are an INSERT (PK liveness "
        "@ts1000) and an all-columns UPDATE (v=2 @ts2000, NO row liveness); the early-stop "
        "returns the row from the UPDATE sstable carrying no PK liveness and never reaches the "
        "older INSERT sstable that holds it. Merged with cass-1's column DELETE (tombstone "
        "@ts3000) the coordinator then sees a row with no live cell AND no PK liveness and drops "
        "it entirely, returning 0 rows instead of the correct `row(pk, ck, null)`. (`SELECT v` "
        "of the single column on the same state correctly returns 1 row with v=null, proving the "
        "row data is present and only `SELECT *` regresses.) The fix (3.11.11) checks "
        "row.primaryKeyLivenessInfo().isEmpty() before treating the all-columns UPDATE row as "
        "complete, so the read does not stop early and the PK liveness is preserved. Component: "
        "Legacy/Local Write-Read Paths."
    )

    # Full multi-node reproduction steps (from the evidence log). This is NOT a runnable
    # single-cluster CQL block: it requires a 2-node ring with per-node gossip isolation,
    # per-node CONSISTENCY ONE writes, and per-node `nodetool flush` so cass-0 ends up with TWO
    # divergent sstables (INSERT + all-columns UPDATE) and cass-1 with a column tombstone.
    # Encoded here verbatim so a future multi-node harness can execute it; the single-cluster
    # GenericCustomBuildProblem injector cannot run it as-is.
    reproducer = """
-- STUB: 2-node ring (RF=2 SimpleStrategy), cassandra:3.11.10. Requires per-replica divergence
-- via gossip isolation + per-node flush; cannot be flattened into a single-cluster CQL
-- reproducer (a single cluster has one merged dataset and CANNOT split the INSERT/UPDATE onto
-- cass-0 and the DELETE onto cass-1, which is what the bug requires).

-- ============================================================================
-- CLUSTER PRECONDITIONS (namespace repro-16671)
--   * 2-node StatefulSet `cass` (cass-0 = Node1, cass-1 = Node2), image cassandra:3.11.10,
--     ephemeral storage.
--   * Both nodes UN (up/normal) before reproduction; schema created while both UN.
--   * hinted_handoff disabled on both pods (`nodetool disablehandoff`) so the per-replica
--     divergence is never erased by hint replay.
-- ============================================================================

-- SCHEMA (run once via any coordinator while both nodes are UN):
CREATE KEYSPACE ks16671 WITH replication = {'class':'SimpleStrategy','replication_factor':2};
CREATE TABLE ks16671.tbl (pk int, ck text, v int, PRIMARY KEY (pk, ck));

-- ============================================================================
-- STEP A — gossip isolation: clean bidirectional DN
-- ============================================================================
--   `nodetool disablehandoff` + `nodetool disablegossip` on BOTH nodes.
--   NOTE: disablegossip freezes a node's own failure-detector evaluation, so cass-0 would
--   never convict cass-1 while its own gossip is off. Fix: re-enable gossip on cass-0 only
--   (cass-1 stays silent) so cass-0's FD convicts cass-1, then complete the writes with each
--   node seeing its peer as DN. Final state before the isolated writes:
--     cass-0 view:  DN <cass-1 ip>   UN <cass-0 ip>
--     cass-1 view:  UN <cass-1 ip>   DN <cass-0 ip>

-- ============================================================================
-- STEP B — isolated divergent writes (each at CONSISTENCY ONE, local-only, handoff disabled)
-- ============================================================================
--   On cass-0 (cqlsh), CONSISTENCY ONE so the writes land on cass-0 only, flushing after each
--   so cass-0 ends up with TWO sstables (INSERT sstable WITH PK liveness, UPDATE sstable with
--   NO row liveness):
CONSISTENCY ONE;
INSERT INTO ks16671.tbl (pk, ck, v) VALUES (1, '1', 1) USING TIMESTAMP 1000;
-- (on cass-0)  nodetool flush ks16671            => sstable #1: clustering ["1"], liveness_info @ts1000, v=1
UPDATE ks16671.tbl USING TIMESTAMP 2000 SET v = 2 WHERE pk = 1 AND ck = '1';
-- (on cass-0)  nodetool flush ks16671            => sstable #2: clustering ["1"], v=2 @ts2000, NO liveness_info

--   On cass-1 (cqlsh), CONSISTENCY ONE so the DELETE lands on cass-1 only, then flush so
--   cass-1 holds ONLY the column tombstone (no live v cell):
CONSISTENCY ONE;
DELETE v FROM ks16671.tbl USING TIMESTAMP 3000 WHERE pk = 1 AND ck = '1';
-- (on cass-1)  nodetool flush ks16671            => sstable: clustering ["1"], v deletion_info @ts3000

-- Physical isolation (sstabledump-verified in the evidence log):
--   cass-0 = 2 sstables (INSERT @ts1000 + all-columns UPDATE @ts2000), NO tombstone.
--   cass-1 = 1 sstable  (column DELETE tombstone @ts3000), NO live v cell.

-- ============================================================================
-- STEP C — re-enable gossip on cass-1; both nodes back to UN (divergence preserved)
-- ============================================================================
--   On cass-1:  nodetool enablegossip      (both UN again)

-- ============================================================================
-- MERGED TRUTH (what a correct coordinator must return):
--   The row (pk=1, ck='1') still exists: cass-0's INSERT set its PK liveness @ts1000, and
--   cass-1 only deleted the v COLUMN (@ts3000). The correct merged result is row(1, '1', null).
-- ============================================================================

-- BUGGY SIGNATURE — the money query (run on coordinator cass-1, CL=ALL, FIRST read):
CONSISTENCY ALL;
SELECT * FROM ks16671.tbl WHERE pk = 1 AND ck = '1';
--   Correct (fixed 3.11.11):  1 | 1 | null   (1 rows)
--   Buggy   (3.11.10):        (0 rows)        <-- the row is WRONGLY DROPPED
--
-- Contrast on the SAME buggy node (proves only SELECT * regresses):
CONSISTENCY ALL;
SELECT v FROM ks16671.tbl WHERE pk = 1 AND ck = '1';
--   Both versions:            null            (1 rows)
"""

    # Deliberately NOT set (stub): continuous_reproducer stays False and expected_output stays
    # None. This IS a wrong-result bug (0 rows instead of `row(1,'1',null)`), but the
    # "wrong-result => set expected_output" rule only applies to RUNNABLE single-cluster
    # reproducers. The reproducer above needs a real 2-node ring with per-replica divergence;
    # looping the SELECT against the single cluster the standard deploy provides would NOT
    # reproduce the 0-rows result (the cluster holds one correctly-merged dataset), so arming
    # the ReproducerPodMitigationOracle via expected_output would falsely report the bug
    # "reproduced/mitigated". This stub contributes only the diagnosis oracle (root cause)
    # until a multi-node harness exists. (Mirrors auto_cassandra_15459, the same shape.)
    continuous_reproducer = False
