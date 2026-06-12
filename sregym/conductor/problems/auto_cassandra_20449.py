"""STUB: multi-node reproduction not yet encoded as a single-cluster Problem — see steps below.

CASSANDRA-20449: Serialization can lose complex deletions in a mutation with multiple collections in a row.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-20449
Buggy: 5.0.3   ->   Fixed: 5.0.4 (also 6.0-alpha1, 6.0)
Components: Legacy/Local Write-Read Paths, Local/Commit Log
Fix commit: 1d47fab638e16e103cbeb19fe979806c16b26b45 (PRs #3987, #3992)

Reproduction summary (a MULTI-NODE RING scenario — NOT a single fresh node / single CQL):
A 2-node ring (both NORMAL/UN), keyspace ks20449 RF=2 (NetworkTopologyStrategy dc1:2) so BOTH
nodes are replicas, table created WITH read_repair = 'NONE'. A single UPDATE mutates THREE
collection columns in one row where one is a set REPLACEMENT (SET s2 = {2}, which emits a complex
deletion / collection-level tombstone) and the other two are APPENDS (s1 = s1 + {2}, s3 = s3 + {2}).
The coordinator (cass-0) applies the mutation correctly from its in-memory object, but the
SERIALIZED copy sent to the peer DROPS the s2 complex deletion, so the peer keeps the older
INSERT-time collection tombstone and merges the stale element {1} into the replacement -> the peer's
s2 = {1, 2} instead of {2}. read_repair='NONE' keeps the divergence from being healed in the
background. The bug is observable ONLY via per-replica reads (each pod's own cqlsh at CONSISTENCY ONE
= executeInternal-style, with no coordinator reconciliation) and per-node sstabledump after a per-node
nodetool flush — any coordinator read at CL > ONE reconciles and heals it via the higher-timestamp
tombstone, and even a CL ONE read is routed non-deterministically to either replica.

WHY THIS IS A STUB (do not flatten into one CQL / one fixed-image sequence):
The GenericCustomBuildProblem lifecycle deploys exactly one db_version (5.0.3) as a SINGLE cluster and
runs the `reproducer` as a CQL string fed to `cqlsh {svc}` against it. This bug fundamentally needs at
least TWO replica nodes AND a per-replica observation that a coordinator-level CQL query cannot make:
the wrong value (s2={1,2}) lives only on the PEER that received the serialized mutation; the
coordinator's own copy is correct. There is NO coordinator CQL that deterministically returns or
persists {1,2} — CL ONE routes to either replica (the evidence log's tooling note: the CL ONE read
"did not deterministically map to cass-0 vs cass-1"), and any CL > ONE read reconciles and HEALS the
divergence via the UPDATE's higher-timestamp tombstone (`read_repair='NONE'` only disables background
read-repair, not read-time reconciliation). The authoritative signature is a per-node sstabledump cell
dump (or a per-replica executeInternal read), not a CQL result set. So the full multi-node steps are
transcribed below and `continuous_reproducer` is left False (a cqlsh-loop reproducer pod could not
stand up two replicas, do per-node flush+sstabledump, and would be a no-op false-pass). See the
authoritative evidence log: .claude/repro-evidence/repro-CASSANDRA-20449.md

Verbatim buggy signature (from the reproduction evidence log):
  -- per-replica CL ONE reads after the UPDATE (the ONLY differing cell is s2):
       NODE A (s2 correct = {2}):   0 | 0 | {1, 2} | {2}    | {1, 2}
       NODE B (s2 WRONG    = {1,2}): 0 | 0 | {1, 2} | {1, 2} | {1, 2}   <-- BUG
  -- cass-1 (buggy peer) sstabledump s2 cells after `nodetool flush ks20449 multi_collection`:
       { "name" : "s2", "deletion_info" : { "marked_deleted" : "2026-06-12T07:56:58.389227Z", ... } },
       { "name" : "s2", "path" : [ "1" ], "value" : "" },
       { "name" : "s2", "path" : [ "2" ], "value" : "", "tstamp" : "2026-06-12T07:56:58.402798Z" }
  -- i.e. s2's complex-deletion ts = .389227Z (the STALE INSERT-time base ts, NOT the UPDATE's
     .402797Z), so the old element [1] SURVIVED alongside [2] => s2 = {1, 2}. The complex deletion that
     accompanied the s2 replacement was dropped during serialization to this peer.
  A/B control (fixed 5.0.4): both replicas read s2 = {2}; the cass-1 sstabledump shows s2's
  complex-deletion ts = the UPDATE's ts and only element [2] present.
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra20449(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "5.0.3"
    source_git_ref = "cassandra-5.0.3"
    # 5.0.3 already ships the bug (fix is 5.0.4), so deploy the stock image instead of
    # running a ~30-min `ant jar` source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/db/rows/BTreeRow.java"
    root_cause_description = (
        "Serialization can lose complex deletions in a mutation that touches multiple collection "
        "(complex) columns in one row. When a Row is serialized for inter-node delivery, "
        "UnfilteredSerializer first asks BTreeRow.hasComplexDeletion() whether to emit the "
        "complex-deletion (collection tombstone) for each complex column. In the buggy 5.0.3 code that "
        "method walks the row's complex columns with a BTree `accumulate` whose accumulator "
        "(`(cd, v) -> complexDeletion().isLive() ? 0 : Cell.MAX_DELETION_TIME`) IGNORES its running "
        "value `v` and returns Cell.MAX_DELETION_TIME only when THAT column has a complex deletion, "
        "with no early stop. So the final result is whatever the LAST-evaluated complex column returns, "
        "not an OR across all of them. With several collection columns where one is a REPLACEMENT "
        "(SET s2={2}, which carries a complex deletion) and a later one is an APPEND (s3=s3+{2}, no "
        "complex deletion), s2 yields MAX but s3 — evaluated after it in column order (s1,s2,s3) — "
        "returns 0 and overwrites that, so hasComplexDeletion() wrongly returns false and the s2 "
        "complex deletion is omitted from the serialized mutation sent to the peer. The coordinator "
        "applies the mutation correctly from its in-memory object, but the peer that receives the "
        "serialized copy keeps the older INSERT-time collection tombstone and merges the stale element "
        "{1} into the replacement, so it persists s2={1,2} instead of {2}. The fix (5.0.4, commit "
        "1d47fab638e16e103cbeb19fe979806c16b26b45) corrects the hasComplexDeletion() accumulation "
        "(renaming the sentinel to STOP_SENTINEL_VALUE and adding the early stop the buggy code "
        "lacked) so the presence of ANY complex column's deletion is preserved and serialized."
    )

    # STUB: this is a MULTI-NODE RING reproducer. It deliberately needs at least TWO replica nodes
    # (RF=2, both replicas) so that the coordinator applies the mutation correctly while the PEER that
    # receives the serialized copy diverges, and the signature is a per-replica read / per-node
    # sstabledump — NOT a coordinator CQL result. It CANNOT be expressed as a single deployed image +
    # single CQL string fed through `cqlsh {svc}`. The full phases from the evidence log are recorded
    # here; do NOT collapse this into a single CQL block run through the generic reproducer path (it
    # would compile and register but silently fail to reproduce — or even observe — the bug, since a
    # coordinator read at CL>ONE heals the divergence and CL ONE is routed non-deterministically).
    reproducer = """
-- ============================================================================
-- STUB / TODO: multi-node ring reproduction — NOT a single CQL, NOT runnable
-- through the single-image deploy->inject->reproduce lifecycle.
--
-- Requires a 2-node single-DC ring where BOTH nodes are replicas, so that one
-- node (the coordinator) applies the multi-collection mutation correctly while
-- the PEER receives the *serialized* copy that has dropped the s2 complex
-- deletion. The bug lives only on the peer and is invisible to a normal
-- coordinator read; observe it per-replica.
--
--   * cass-0, cass-1 : two NORMAL members (UN), e.g. a StatefulSet replicas=2,
--                      snitch=GossipingPropertyFileSnitch, single DC `dc1`.
--   * keyspace       : ks20449 RF=2 via NetworkTopologyStrategy {'dc1':2} so
--                      BOTH nodes are replicas of every row.
--   * table          : created WITH read_repair = 'NONE' so the divergence is
--                      not silently healed by background read-repair.
-- ============================================================================

-- PHASE 1 — bring up the 2-node ring; confirm both NORMAL before writing.
-- shell: kubectl exec cass-0 -- nodetool status   ->   2x UN
-- Create the keyspace (RF=2, both replicas) and the table with read_repair='NONE':
CREATE KEYSPACE IF NOT EXISTS ks20449 WITH replication = {'class':'NetworkTopologyStrategy','dc1':2};
CREATE TABLE IF NOT EXISTS ks20449.multi_collection (
    k int, c int, s1 set<int>, s2 set<int>, s3 set<int>,
    PRIMARY KEY (k, c)
) WITH read_repair = 'NONE';

-- PHASE 2 — drive the workload through the coordinator cass-0 at CONSISTENCY ALL
--           (so both replicas receive both mutations). `CONSISTENCY ALL;` is a
--           cqlsh session command (NOT inline `USING CONSISTENCY` CQL).
-- shell: kubectl exec cass-0 -- cqlsh -f <workload>.cql
CONSISTENCY ALL;
INSERT INTO ks20449.multi_collection (k, c, s1, s2, s3) VALUES (0, 0, {1}, {1}, {1});
-- The trigger: ONE UPDATE mutating three collections in the same row — s2 is a
-- set REPLACEMENT (emits a complex deletion / collection tombstone), s1 and s3
-- are APPENDs. On 5.0.3 the s2 complex deletion is lost when the mutation is
-- serialized to the peer.
UPDATE ks20449.multi_collection SET s2 = {2}, s1 = s1 + {2}, s3 = s3 + {2} WHERE k = 0 AND c = 0;

-- PHASE 3 — OBSERVE per-replica (a coordinator read would heal/hide the bug).
-- (a) Per-replica CL ONE reads — connect to EACH pod's OWN cqlsh and read at
--     CONSISTENCY ONE (mimics executeInternal: a pure per-replica read with no
--     coordinator reconciliation). NOTE: CL ONE is routed by the snitch to
--     whichever replica is "closest", so which physical pod returns {1,2} is
--     non-deterministic — this is why a single coordinator CQL cannot serve as a
--     deterministic reproducer; use sstabledump (b) as the authoritative proof.
-- shell: kubectl exec cass-0 -- cqlsh -f read-one.cql   # CONSISTENCY ONE; SELECT ...
-- shell: kubectl exec cass-1 -- cqlsh -f read-one.cql   # CONSISTENCY ONE; SELECT ...
CONSISTENCY ONE;
SELECT k, c, s1, s2, s3 FROM ks20449.multi_collection WHERE k = 0 AND c = 0;
--   One replica returns s2 = {2}        (correct)
--   The other returns  s2 = {1, 2}      (BUG — old element {1} survived the replacement)
--   s1 and s3 are {1, 2} on BOTH nodes (appends, correctly preserved).

-- (b) Authoritative physical proof — flush each node and dump its local SSTable:
-- shell: kubectl exec cass-0 -- nodetool flush ks20449 multi_collection
-- shell: kubectl exec cass-1 -- nodetool flush ks20449 multi_collection
-- shell: kubectl exec cass-{0,1} -- /opt/cassandra/tools/bin/sstabledump <...-Data.db>
--   (sstabledump is NOT on PATH in cassandra:5.0.x; it lives at
--    /opt/cassandra/tools/bin/sstabledump)
--
--   Correct replica s2 cells:
--     { "name":"s2", "deletion_info":{ "marked_deleted":"<UPDATE ts e.g. ...402797Z>", ... } },
--     { "name":"s2", "path":["2"], "value":"", "tstamp":"<...402798Z>" }
--       -> only element [2] survives => s2 = {2}.
--
--   BUGGY peer s2 cells (the node that received the serialized mutation):
--     { "name":"s2", "deletion_info":{ "marked_deleted":"<STALE INSERT ts e.g. ...389227Z>", ... } },
--     { "name":"s2", "path":["1"], "value":"" },
--     { "name":"s2", "path":["2"], "value":"", "tstamp":"<...402798Z>" }
--       -> s2's complex-deletion ts is the STALE INSERT-time base ts (NOT the
--          UPDATE's ts), so old element [1] SURVIVED alongside [2] => s2 = {1, 2}.
--          The s2 complex deletion was dropped during serialization to this peer.
--
-- BUGGY (5.0.3): the peer persists s2 = {1, 2}; replicas diverge on s2 only.
-- FIXED  (5.0.4): identical workload -> the s2 complex deletion is serialized to
--                 the peer, both replicas persist s2 = {2}, no divergence.
"""

    # STUB: no working single-cluster looping reproducer pod. The single-image
    # deploy->inject->reproduce lifecycle deploys one node/one cluster and runs the
    # reproducer as CQL through `cqlsh {svc}`; it cannot stand up two replicas, route
    # the coordinator vs. serialized-peer roles, and read per-node sstabledump / per-replica
    # CL ONE. Left False so a non-functional continuous reproducer is not falsely advertised
    # (a CQL-loop pod running the prose above would be a no-op / false pass — and a coordinator
    # read would heal the divergence, never seeing the bug).
    continuous_reproducer = False
    # NOTE: although this is "wrong-result" in flavor (the peer PERSISTS s2={1,2} instead of {2}),
    # expected_output is intentionally UNSET. The buggy value {1,2} is a PER-REPLICA persisted value
    # observable only via a per-node sstabledump (or an executeInternal-style CL ONE read routed to the
    # buggy replica) — it is NOT a value any coordinator CQL query deterministically returns (the
    # coordinator's own copy is correct, CL ONE is routed non-deterministically, and CL>ONE reconciles
    # and heals it via the higher-timestamp tombstone). expected_output only feeds the continuous
    # ReproducerPodMitigationOracle (not armed here, since continuous_reproducer=False), so setting it
    # would be misleading.
