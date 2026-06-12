"""CASSANDRA-15459: Short read protection doesn't work on group-by queries.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-15459

Buggy: 3.11.7  ->  Fixed: 3.11.8 (also 4.0-beta2 / 4.0).

STUB: multi-node reproduction not yet encoded as a single-cluster Problem — see steps below.

Reproduction summary (2-node ring, RF=2, coordinator-side bug):
  On a two-node cluster with RF=2, per-replica divergence is injected via gossip
  isolation (nodetool disablegossip on the peer) so CONSISTENCY ONE writes land on
  only one replica, with hinted handoff disabled so the divergence is preserved.
  Node1 (cass-0) gets INSERT(pk=1,c=1)@ts9, DELETE(pk=0,c=0)@ts10, INSERT(pk=2,c=2)@ts9;
  Node2 (cass-1) gets DELETE(pk=1,c=1)@ts10, INSERT(pk=0,c=0)@ts9, DELETE(pk=2,c=2)@ts10.
  After gossip is re-enabled (both UN), a coordinator running
  `CONSISTENCY ALL; SELECT pk, c FROM k15459.t GROUP BY pk LIMIT 1` must merge the two
  replicas — every partition's delete@ts10 beats its insert@ts9, so the correct merged
  result is (0 rows). The buggy 3.11.7 coordinator's Short Read Protection recomputes
  the LIMIT using a ROW count instead of a GROUP count, short-circuits early, and
  surfaces a deleted row. (The exact wrong row shifts as blocking read-repair reconciles
  state, e.g. [0,0] on the first run then [2,2] — characteristic of SRP miscounting
  groups as rows, NOT a fixed stale read.)

This bug is coordinator-side merge logic over two divergent replicas; it CANNOT manifest
on a single node and CANNOT be expressed as one single-cluster CQL string. It therefore
requires multi-pod orchestration (per-node gossip isolation + per-node CL ONE writes) that
is not yet available in the single-cluster GenericCustomBuildProblem harness. The full
reproducer steps from the evidence log are preserved in the `reproducer` field below so
this can be promoted to a real multi-node Problem later.

Verbatim buggy signature (cassandra:3.11.7, first run):
  Consistency level set to ALL.

   pk | c
  ----+---
    0 | 0

  (1 rows)

  Warnings :
  Aggregation query used without partition key

Fixed 3.11.8 returns (0 rows) for the identical workload and query.
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra15459(GenericCustomBuildProblem):
    # STUB: see module docstring. Multi-node (2-node ring, RF=2) coordinator-side bug
    # that cannot be reproduced by a single-cluster CQL reproducer. Fields below are set
    # so the problem registers and carries the root-cause + full multi-node steps, but the
    # `reproducer` is NOT a runnable single-cluster CQL block (no continuous_reproducer,
    # no expected_output — deliberately, to avoid a false "it works" mitigation oracle).
    db_name = "cassandra"
    db_version = "3.11.7"
    source_git_ref = "cassandra-3.11.7"
    # 3.11.7 already ships the bug (buggy = fix patch 3.11.8 - 1), so deploy the stock image.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/service/DataResolver.java"
    root_cause_description = (
        "Coordinator-side Short Read Protection (SRP) does not work for GROUP BY queries. "
        "When per-replica divergence causes a `GROUP BY ... LIMIT` query to short-read, the "
        "SRP path in DataResolver recomputes the remaining limit using a ROW count instead of "
        "a GROUP count, so it stops fetching early and surfaces a partition (e.g. pk=0) that is "
        "actually deleted on every replica once correctly merged (every delete@ts10 beats its "
        "insert@ts9). The correct merged result is (0 rows); the buggy coordinator returns a "
        "deleted row. Component: Legacy/Coordination. Fixed in 3.11.8 / 4.0-beta2 / 4.0."
    )

    # Full multi-node reproduction steps (from the evidence log). This is NOT a runnable
    # single-cluster CQL block: it requires a 2-node ring with per-node gossip isolation and
    # per-node CONSISTENCY ONE writes. Encoded here verbatim so a future multi-node harness can
    # execute it; the single-cluster GenericCustomBuildProblem injector cannot run it as-is.
    reproducer = """
-- STUB: 2-node ring (RF=2), cassandra:3.11.7. Requires per-replica divergence via gossip
-- isolation; cannot be flattened into a single-cluster CQL reproducer.

-- ============================================================================
-- CLUSTER PRECONDITIONS
--   * 2-node StatefulSet `cass` (cass-0 = Node1, cass-1 = Node2), image cassandra:3.11.7.
--   * Both nodes UN (up/normal) before reproduction.
--   * hinted_handoff_enabled: false baked into cassandra.yaml on both nodes, AND
--     `nodetool disablehandoff` run on both pods (belt-and-suspenders) so divergence is
--     never erased by hint replay.
-- ============================================================================

-- SCHEMA (run once via any coordinator):
CREATE KEYSPACE k15459 WITH replication = {'class':'SimpleStrategy','replication_factor':2};
CREATE TABLE k15459.t (pk int, c int, PRIMARY KEY (pk, c))
  WITH read_repair_chance = 0 AND dclocal_read_repair_chance = 0;

-- ============================================================================
-- STEP A — isolate cass-1, write NODE1-only data on cass-0 at CONSISTENCY ONE
-- ============================================================================
--   On cass-1:  nodetool disablegossip      (cass-0 now sees cass-1 as DN)
--   On cass-0 (cqlsh), at CONSISTENCY ONE so writes land on cass-0 only:
CONSISTENCY ONE;
INSERT INTO k15459.t (pk, c) VALUES (1, 1) USING TIMESTAMP 9;
DELETE FROM k15459.t USING TIMESTAMP 10 WHERE pk = 0 AND c = 0;
INSERT INTO k15459.t (pk, c) VALUES (2, 2) USING TIMESTAMP 9;
--   On cass-0:  nodetool flush k15459
--   (cass-0 local CL ONE state: rows (1,1) and (2,2) live; pk=0 is a tombstone@ts10)

-- ============================================================================
-- STEP B — re-enable gossip on cass-1, isolate cass-0, write NODE2-only data on cass-1
-- ============================================================================
--   On cass-1:  nodetool enablegossip
--   On cass-0:  nodetool disablegossip      (cass-1 now sees cass-0 as DN)
--   On cass-1 (cqlsh), at CONSISTENCY ONE so writes land on cass-1 only:
CONSISTENCY ONE;
DELETE FROM k15459.t USING TIMESTAMP 10 WHERE pk = 1 AND c = 1;
INSERT INTO k15459.t (pk, c) VALUES (0, 0) USING TIMESTAMP 9;
DELETE FROM k15459.t USING TIMESTAMP 10 WHERE pk = 2 AND c = 2;
--   On cass-1:  nodetool flush k15459
--   (cass-1 local CL ONE state: row (0,0) live only)

-- ============================================================================
-- STEP C — re-enable gossip on cass-0; both nodes back to UN (divergence preserved)
-- ============================================================================
--   On cass-0:  nodetool enablegossip

-- ============================================================================
-- MERGED TRUTH (what a correct coordinator must return):
--   pk=0: INSERT@9 (cass-1) vs DELETE@10 (cass-0) -> DELETE wins -> DEAD
--   pk=1: INSERT@9 (cass-0) vs DELETE@10 (cass-1) -> DELETE wins -> DEAD
--   pk=2: INSERT@9 (cass-0) vs DELETE@10 (cass-1) -> DELETE wins -> DEAD
--   => all partitions dead; correct GROUP BY pk LIMIT 1 result is (0 rows).
-- ============================================================================

-- BUGGY SIGNATURE — the money query (run on the coordinator, e.g. cass-0):
CONSISTENCY ALL;
SELECT pk, c FROM k15459.t GROUP BY pk LIMIT 1;
--   Correct (fixed 3.11.8): (0 rows).
--   Buggy (3.11.7): returns a DELETED row, e.g. [0, 0] with (1 rows) on the first run,
--   then [2, 2] after blocking read-repair partially reconciles state — the wrong row
--   shifts, confirming SRP miscounts groups as rows rather than returning a stale value.
"""

    # Deliberately NOT set (stub): continuous_reproducer stays False and expected_output
    # stays None. The reproducer above is not runnable as single-cluster CQL, so attaching a
    # looping reproducer pod / mitigation oracle would falsely report "reproduced". This stub
    # contributes only the diagnosis oracle (root cause) until a multi-node harness exists.
