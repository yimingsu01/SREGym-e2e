"""CASSANDRA-20189: Avoid possible consistency violations for SAI intersection queries
over repaired index matches and multiple non-indexed column matches.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-20189
Buggy: 5.0.3   Fixed: 5.0.4, 6.0
Components: Consistency/Coordination, Feature/SAI

STUB: multi-node reproduction not yet encoded as a single-cluster Problem — see steps below.

Reproduction summary (2-node ring, RF=2, read_repair='NONE', SAI index on column a,
hinted_handoff disabled):
  1. INSERT (k=0, a=1) at CL ALL, flush both nodes, then run an INCREMENTAL repair so the
     a=1 sstable is marked repairedAt>0 on BOTH replicas (the "repaired index match").
  2. Split the row across replicas via gossip isolation + CL ONE: write b=2 to cass-0 only
     and c=3 to cass-1 only, then flush both nodes. No single replica holds both b and c.
  3. SELECT * WHERE a=1 AND b=2 AND c=3 ALLOW FILTERING at CL ALL. FilterTree applies strict
     per-replica post-filtering because the index column returned only repaired matches while
     MULTIPLE non-indexed columns (b, c) still need filtering — so each replica is filtered
     before coordinator reconciliation, neither alone satisfies a AND b AND c, and the
     reconciled row is silently dropped (a CL=ALL read returns fewer rows than the data holds).

VERBATIM BUGGY SIGNATURE (buggy 5.0.3):
  The SAI intersection query at CONSISTENCY ALL returns:
      (0 rows)
  while the identical-CL primary-key read (SELECT * WHERE k=0) returns the row:
       k | a | b | c
      ---+---+---+---
       0 | 1 | 2 | 3
      (1 rows)
  The absence of the row from the SAI intersection result IS the bug signature. On fixed
  5.0.4 the identical SAI intersection query returns the row (1 rows).

Why this is a STUB and not a flattened single-cluster `reproducer` CQL string:
  The mechanism IS per-replica divergence (b=2 on cass-0, c=3 on cass-1) produced by
  gossip isolation + CL ONE writes, gated by an incremental repair that marks sstables
  repairedAt>0 on both replicas. This consistency violation cannot exist on a single node
  and cannot be expressed as one CQL string against one cluster: it needs multi-pod
  orchestration (per-node `nodetool disablegossip/enablegossip`, per-node `flush`, a ring
  `repair`, and CL ONE writes routed through individual live nodes). A flattened single-CQL
  version would compile and register but silently NOT reproduce the bug — worse than an
  honest stub. The full multi-node steps from the evidence log are preserved in `reproducer`
  below for a future multi-node Problem implementation.
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra20189(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "5.0.3"
    source_git_ref = "cassandra-5.0.3"
    # 5.0.3 already ships the bug (fix lands in 5.0.4), so deploy the stock image instead of
    # running a ~30-min ant-jar source build.
    prebuilt_from_stock = True

    # NOTE: the evidence log names `FilterTree` (SAI post-filtering) as the culprit but gives
    # no source path, and no 5.0.x source tree was available locally to confirm it. This path
    # is the SAI plan package where FilterTree lives in Cassandra 5.0; it is DERIVED from the
    # log's FilterTree reference, not verified against a 5.0.3 checkout.
    root_cause_file = "src/java/org/apache/cassandra/index/sai/plan/FilterTree.java"
    root_cause_description = (
        "FilterTree (SAI post-filtering) is too aggressive about using strict per-replica "
        "filtering when (a) only repaired matches are returned from the indexed column and "
        "(b) multiple non-indexed columns must still be post-filtered. Strict filtering "
        "evaluates the non-indexed predicates on each replica BEFORE coordinator "
        "reconciliation. When the values satisfying those predicates are split across "
        "replicas (node1 holds b=2, node2 holds c=3), no single replica satisfies all "
        "predicates, so the row is silently dropped — a CL=ALL read returns fewer rows than "
        "the reconciled data contains (a consistency violation). The fix avoids strict "
        "filtering in this repaired-index-match + multiple-non-indexed-match case."
    )

    # STUB reproducer: the FULL multi-node steps from the evidence log. These are NOT runnable
    # as a single CQL block — they require per-node nodetool/gossip orchestration across a
    # 2-node ring (cass-0, cass-1). Preserved verbatim for a future multi-node Problem.
    reproducer = """
-- STUB: multi-node reproduction not yet encoded as a single-cluster Problem.
-- Requires a 2-node Cassandra ring (StatefulSet cass-0, cass-1), RF=2
-- (NetworkTopologyStrategy dc1:2), ephemeral storage, GossipingPropertyFileSnitch,
-- and `hinted_handoff_enabled: false` appended to cassandra.yaml (so the isolated
-- node cannot heal the split via hints). This is the real-ring translation of the
-- in-JVM dtest `testPartialUpdatesOnNonIndexedColumnsAfterRepair`.

-- Schema (RF=2, read_repair NONE, SAI index on the indexed column a):
CREATE KEYSPACE IF NOT EXISTS repro20189
  WITH REPLICATION = {'class': 'NetworkTopologyStrategy', 'dc1': 2};
CREATE TABLE IF NOT EXISTS repro20189.partial_updates (
  k int PRIMARY KEY,
  a int,
  b int,
  c int
) WITH read_repair = 'NONE';
CREATE INDEX IF NOT EXISTS partial_updates_a_idx
  ON repro20189.partial_updates (a) USING 'sai';

-- Step 1: repaired index match.
--   cqlsh> CONSISTENCY ALL;
--   INSERT INTO repro20189.partial_updates (k, a) VALUES (0, 1) USING TIMESTAMP 1;
--   (cass-0) nodetool flush repro20189
--   (cass-1) nodetool flush repro20189
--   nodetool repair repro20189        # incremental:true -> marks the a=1 sstable
--                                       # repairedAt>0 on BOTH replicas.

-- Step 2: split the row across replicas via gossip isolation + CL ONE.
--   # write b=2 to cass-0 ONLY:
--   (cass-1) nodetool disablegossip   # poll until cass-0 sees cass-1 = DN
--   (cass-0) cqlsh> CONSISTENCY ONE;
--            INSERT INTO repro20189.partial_updates (k, b) VALUES (0, 2) USING TIMESTAMP 2;
--   (cass-1) nodetool enablegossip
--   # write c=3 to cass-1 ONLY:
--   (cass-0) nodetool disablegossip   # poll until cass-1 sees cass-0 = DN
--   (cass-1) cqlsh> CONSISTENCY ONE;
--            INSERT INTO repro20189.partial_updates (k, c) VALUES (0, 3) USING TIMESTAMP 3;
--   (cass-0) nodetool enablegossip
--   (cass-0) nodetool flush repro20189
--   (cass-1) nodetool flush repro20189
--   # physical split now: cass-0 holds {a=1, b=2}; cass-1 holds {a=1, c=3}.

-- Step 3: the violation, both queries at CONSISTENCY ALL.
--   # PK read (no strict filtering) proves the reconciled row exists:
SELECT * FROM repro20189.partial_updates WHERE k = 0;
--     -> 0 | 1 | 2 | 3   (1 rows)
--   # SAI intersection filter query (multiple non-indexed predicates) is the bug:
SELECT * FROM repro20189.partial_updates
  WHERE a = 1 AND b = 2 AND c = 3 ALLOW FILTERING;
--     -> (0 rows)  on buggy 5.0.3   [should be 0 | 1 | 2 | 3, as on fixed 5.0.4]
"""

    # Multi-node stub: NOT a runnable single-cluster reproducer, so do not wire up the
    # looping ReproducerPodMitigationOracle (it would deploy a pod running these nodetool/
    # gossip steps as CQL and never function). Diagnosis oracle only.
    continuous_reproducer = False
    # No expected_output: the buggy result is an ABSENT row ((0 rows)), which a presence-grep
    # readiness probe cannot detect — and this stub never runs a continuous reproducer anyway.
