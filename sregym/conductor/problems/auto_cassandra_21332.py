"""Queries on Static SAI-indexed Columns May Resurrect Range Tombstoned Data During Replica Filtering Protection

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-21332
Buggy: 5.0.8  ->  Fixed: 5.0.9 (also 6.0-alpha2, 7.x)

STUB: multi-node reproduction not yet encoded as a single-cluster Problem — see steps below.

Reproduction summary (3-node ring, RF=3):
A static SAI-indexed column with read_repair='NONE' is queried via the coordinator with
CONSISTENCY ALL. Each replica holds DIFFERENT data for the SAME partition key (pk0=1) — an
invariant the normal coordinator/CQL write path CANNOT produce, so it must be staged by
isolating each node via `nodetool disablegossip` on the others and writing at CONSISTENCY ONE.
node2 carries a range tombstone (TS2) covering ck0<=true plus the only surviving row (s1=42);
node1 and node3 carry stale rows (TS1) that the tombstone logically deletes. The SAI first-pass
query matches only node2's surviving row; the Replica Filtering Protection (RFP) completion reads
on node1/node3 then re-read the whole partition WITHOUT being supplied node2's range tombstone, so
the logically-deleted stale rows are NOT shadowed and get resurrected.

Verbatim buggy signature (coordinator = node1):
  CONSISTENCY ALL; PAGING 1; SELECT ck0, ck1 FROM rfp21332.rt_static_sai WHERE s1 = 42;
  returns 3 rows [(False,1),(True,4),(True,5)] instead of the single correct row (True,5) —
  range-tombstoned static-SAI rows resurrected during Replica Filtering Protection.

Why this is a STUB and not a flattened single-CQL reproducer:
The bug REQUIRES per-replica divergence on one partition key across 3 nodes. A single cqlsh
reproducer string goes through the coordinator and replication, which keeps all RF replicas
consistent for a given partition — it therefore CANNOT create the diverged state and would
compile/register while silently NOT reproducing the bug. Staging the divergence needs multi-pod
orchestration (gossip isolation + CONSISTENCY ONE writes + flush + re-converge, per node, verified
physically with sstabledump), which a single `reproducer` CQL string cannot express. The full
multi-node steps from the evidence log are recorded verbatim in `reproducer` below.

No A/B control image exists: the fix patch (5.0.9) exceeds the public cassandra:5.0.x Docker Hub
ceiling (5.0.8), so no fixed `cassandra:5.0.9` image is available. The within-version control is:
the normal full-partition read (`SELECT ... WHERE pk0=1`) on the SAME diverged data correctly
returns 1 row (True,5), while only the SAI/RFP path resurrects the tombstoned rows.
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra21332(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "5.0.8"
    source_git_ref = "cassandra-5.0.8"
    # 5.0.8 already ships the bug (fix = 5.0.9), so deploy the stock image instead of an ant-jar build.
    prebuilt_from_stock = True

    # NOTE: inferred root-cause file. The evidence log describes the RFP mechanism (range tombstones
    # from one replica not supplied to the completion reads on the other replicas during the SAI
    # query path) but does NOT name a source file. ReplicaFilteringProtection.java is the component
    # that issues the completion reads, so it is the best-effort location pending confirmation.
    root_cause_file = "src/java/org/apache/cassandra/service/reads/ReplicaFilteringProtection.java"
    root_cause_description = (
        "Queries on a static StorageAttachedIndex (SAI) column with read_repair='NONE' resurrect "
        "range-tombstoned data during Replica Filtering Protection (RFP). The SAI first-pass query "
        "(s1 = 42) matches only the surviving row on the replica that holds the range tombstone; the "
        "RFP completion reads issued against the other replicas re-read the whole partition WITHOUT "
        "being supplied that range tombstone (which lives only on the tombstone-holding replica), so "
        "the logically-deleted stale rows on those replicas are not shadowed and are returned to the "
        "client. The root-cause file is inferred from the RFP completion-read mechanism, not named in "
        "the evidence log."
    )

    # STUB: multi-node reproduction not yet encoded as a single-cluster Problem — see steps below.
    # The bug requires per-replica divergence on the SAME partition key (pk0=1) across 3 nodes, which
    # the normal coordinator/CQL write path CANNOT produce. These are the FULL multi-node steps from
    # the evidence log (NOT a flattened single-cluster CQL — a single cqlsh string would route through
    # the coordinator + replication and silently fail to create the diverged state).
    reproducer = """
-- ============================================================================
-- STUB: 3-node ring, RF=3. Requires multi-pod orchestration (gossip isolation +
-- CONSISTENCY ONE writes + flush + re-converge per node). NOT runnable as a
-- single cqlsh reproducer string. Steps below are recorded verbatim from the
-- reproduction evidence log.
-- Topology: 3 pods, RF=3. dtest node1->cass-0, node2->cass-1, node3->cass-2;
-- coordinator(1) -> cass-0.
-- ============================================================================

-- 1. Schema (matches the in-JVM dtest exactly):
CREATE KEYSPACE rfp21332 WITH replication = {'class':'NetworkTopologyStrategy','dc1':3};
CREATE TABLE rfp21332.rt_static_sai (
  pk0 int, ck0 boolean, ck1 double, s1 int static, v0 boolean,
  PRIMARY KEY (pk0, ck0, ck1)
) WITH read_repair = 'NONE';
CREATE CUSTOM INDEX ON rfp21332.rt_static_sai(s1) USING 'StorageAttachedIndex';

-- 2. Prep on ALL 3 pods (kubectl exec):
--    nodetool disablehandoff
--    nodetool disableautocompaction

-- 3. Stage PER-REPLICA DIVERGENT data on the SAME partition key pk0=1. The dtest
--    uses executeInternal (direct local apply); the kind analog per round is:
--      a) nodetool disablegossip on the OTHER two pods
--      b) poll `nodetool status` from the writer until the others show DN
--      c) inside the writer pod: cqlsh -e "CONSISTENCY ONE; <write> USING TIMESTAMP n"
--      d) nodetool flush on the writer
--      e) nodetool enablegossip on the others; poll until UN=3
--    Verify physically with sstabledump on each node's local Data.db (a CL=ONE read
--    is coordinator-routed and CANNOT confirm a specific node's local state).
--
--    Round A — write to node3 (cass-2): stale row ck0=false @ TS1
INSERT INTO rfp21332.rt_static_sai (pk0, ck0, ck1, s1, v0) VALUES (1, false, 1.0, 99, false) USING TIMESTAMP 1;
--    Round B — write to node1 (cass-0): stale row ck0=true @ TS1
INSERT INTO rfp21332.rt_static_sai (pk0, ck0, ck1, s1, v0) VALUES (1, true, 4.0, 99, false) USING TIMESTAMP 1;
--    Round C — write to node2 (cass-1): range tombstone @ TS2 covering ck0<=true (all)
DELETE FROM rfp21332.rt_static_sai USING TIMESTAMP 2 WHERE pk0 = 1 AND ck0 <= true;
--    Round D — write to node2 (cass-1): the only surviving row, s1=42 @ TS3
INSERT INTO rfp21332.rt_static_sai (pk0, ck0, ck1, s1, v0) VALUES (1, true, 5.0, 42, true) USING TIMESTAMP 3;

-- 4. BUG QUERY via coordinator cass-0 (= dtest coordinator node1), CONSISTENCY ALL,
--    page size 1 (SAI + Replica Filtering Protection path):
CONSISTENCY ALL;
PAGING 1;
SELECT ck0, ck1 FROM rfp21332.rt_static_sai WHERE s1 = 42;
-- EXPECTED (correct, per dtest assertRows): exactly ONE row -> (True, 5.0)
-- BUGGY OBSERVED (5.0.8): 3 rows -> (False,1.0), (True,4.0), (True,5.0)
--   The (False,1.0) and (True,4.0) rows are covered by the range tombstone @ TS2
--   but get RESURRECTED because the tombstone (held only on node2) is not supplied
--   to the RFP completion reads on node1/node3.

-- 5. WITHIN-VERSION CONTROL (no fixed image available): the normal full-partition
--    read on the SAME diverged data correctly returns 1 row, proving only the
--    SAI/RFP path is wrong:
CONSISTENCY ALL;
SELECT pk0, ck0, ck1, s1, v0 FROM rfp21332.rt_static_sai WHERE pk0 = 1;
-- CORRECT: 1 row -> (1, True, 5.0, 42, True)
"""
    # Honest stub: NOT a continuous reproducer. continuous_reproducer=True would wire a
    # ReproducerPodMitigationOracle to a pod running the above as a single cqlsh blob, which
    # cannot create the per-replica divergence and so cannot fire the bug — a meaningless grade.
    continuous_reproducer = False
