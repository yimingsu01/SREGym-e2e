"""CASSANDRA-16334: Replica failure causes timeout on multi-DC write.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16334

Buggy: 4.0.1  ->  Fixed: 4.0.2.
Components: Consistency/Coordination, Messaging/Internode.

STUB: multi-node reproduction not yet encoded as a single-cluster Problem — see steps below.

Reproduction summary (2-DC ring, multi-DC coordinator bug; RF=1/DC is sufficient):
  On a two-datacenter ring (dc1 + dc2, GossipingPropertyFileSnitch) with a
  NetworkTopologyStrategy{dc1:1, dc2:1} keyspace and `max_mutation_size_in_kb: 1000`
  on every node, INSERT a single ~1.1 MB byte blob (0x + 'ab'*1100000 = 2.2 MB hex)
  at CONSISTENCY LOCAL_ONE. The blob exceeds max_mutation_size_in_kb so every replica
  rejects the mutation. On the buggy 4.0.1 multi-DC coordinator path the write wrongly
  surfaces a WriteTimeout (code=1100) instead of the correct WriteFailure (code=1500).
  The SAME image, SAME workload on a SINGLE-DC keyspace (dc1:1) instead returns the
  CORRECT WriteFailure (code=1500) — isolating the defect to the multi-DC path. The
  consistency level MUST be LOCAL_ONE: cqlsh's default ONE routes through the
  all-DC WriteResponseHandler failure count and would mask the bug.

Verbatim buggy signature (from the reproduction evidence log; cassandra:4.0.1, multi-DC):
  WriteTimeout: Error from server: code=1100 [Coordinator node timed out waiting for replica nodes' responses] message="Operation timed out - received only 0 responses." info={'consistency': 'LOCAL_ONE', 'required_responses': 1, 'received_responses': 0, 'write_type': 'SIMPLE'}

Fixed 4.0.2 returns, for the identical 2-DC topology and identical workload:
  WriteFailure: Error from server: code=1500 [Replica(s) failed to execute write] ... 'failures': 1 ...

WHY THIS IS A STUB (do not flatten into one single-cluster CQL block):
The GenericCustomBuildProblem lifecycle deploys exactly ONE Cassandra cluster (a single
datacenter) and runs the `reproducer` CQL against it. This bug only manifests when the
cluster actually has TWO named datacenters (dc1/dc2) under GossipingPropertyFileSnitch
with a NetworkTopologyStrategy{dc1:1, dc2:1} keyspace, AND the write is issued at
CONSISTENCY LOCAL_ONE. RESULT 2 of the evidence log proves the contrast: on the SAME
buggy 4.0.1 image, a SINGLE-DC keyspace returns the CORRECT WriteFailure — so a flattened
single-cluster / SimpleStrategy CQL reproducer would compile and register but SILENTLY
NOT reproduce the bug. A CQL string cannot stand up a second datacenter, and an
inject_fault() override (nodetool/CQL) cannot change the deployed cluster topology either.
The full multi-DC topology + workload from the evidence log is preserved verbatim in
`reproducer` below so this can be promoted to a real multi-node Problem once a multi-DC
harness exists. See the authoritative evidence log:
.claude/repro-evidence/repro-CASSANDRA-16334.md
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra16334(GenericCustomBuildProblem):
    # STUB: see module docstring. Multi-DC (2-datacenter ring) coordinator-side bug that
    # cannot be reproduced by a single-cluster CQL reproducer (the standard deploy gives a
    # single datacenter). Fields below are set so the Problem registers and carries the
    # root-cause + full multi-DC steps, but `reproducer` is NOT a runnable single-cluster
    # CQL block (continuous_reproducer stays False and no expected_output is set, to avoid
    # arming a mitigation oracle that would falsely report "reproduced").
    db_name = "cassandra"
    db_version = "4.0.1"
    source_git_ref = "cassandra-4.0.1"
    # 4.0.1 already ships the bug (buggy = fix patch 4.0.2 - 1), so deploy the stock image
    # instead of running a ~30-min `ant jar` source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/service/AbstractWriteResponseHandler.java"
    root_cause_description = (
        "Replica failure wrongly causes a WriteTimeout instead of a WriteFailure on a "
        "multi-DC write with a DC-local consistency level (LOCAL_ONE). When an oversized "
        "mutation (larger than max_mutation_size_in_kb) is rejected by every replica, the "
        "coordinator's write-response handler counts total replicas across ALL DCs but only "
        "tallies failure responses from the LOCAL DC. The failure-threshold check "
        "`blockFor() + failures > candidateReplicaCount()` in AbstractWriteResponseHandler "
        "therefore never trips (candidateReplicaCount() uses the all-DC replica count while "
        "`failures` only accrues local-DC failures), so the coordinator waits out the full "
        "write timeout and emits WriteTimeout (code=1100) instead of the correct WriteFailure "
        "(code=1500). On a single-DC keyspace, where total replicas == local-DC replicas, the "
        "same workload correctly yields WriteFailure. The fix (4.0.2) makes candidateReplicaCount() "
        "return the local-DC replica count when consistencyLevel().isDatacenterLocal(), so the "
        "all-replicas-rejected case crosses the failure threshold. Components: "
        "Consistency/Coordination, Messaging/Internode."
    )

    # Full multi-DC reproduction steps (from the evidence log). This is NOT a runnable
    # single-cluster CQL block: it requires a 2-datacenter ring (dc1 + dc2) under
    # GossipingPropertyFileSnitch with a NetworkTopologyStrategy keyspace and a write issued
    # at CONSISTENCY LOCAL_ONE. Encoded here verbatim so a future multi-DC harness can execute
    # it; the single-cluster GenericCustomBuildProblem injector cannot run it as-is.
    reproducer = """
-- STUB: 2-datacenter ring (dc1 + dc2), cassandra:4.0.1. Requires a real multi-DC topology
-- (GossipingPropertyFileSnitch) and CONSISTENCY LOCAL_ONE; CANNOT be flattened into a
-- single-cluster / SimpleStrategy CQL reproducer (a single-DC keyspace returns the CORRECT
-- WriteFailure on the SAME buggy image — see RESULT 2 in the evidence log).

-- ============================================================================
-- CLUSTER PRECONDITIONS (cluster name "repro")
--   * Two single-node Cassandra pods, ONE per datacenter:
--       cass-dc1  (datacenter dc1, rack1)   e.g. 10.244.1.130
--       cass-dc2  (datacenter dc2, rack1)   e.g. 10.244.1.131
--   * snitch: GossipingPropertyFileSnitch (cassandra-rackdc.properties sets dc=dc1 / dc=dc2).
--   * `max_mutation_size_in_kb: 1000` appended to cassandra.yaml on BOTH nodes.
--   * Both nodes UN (up/normal) across both datacenters before reproduction:
--       Datacenter: dc1 ... UN  10.244.1.130 ... rack1
--       Datacenter: dc2 ... UN  10.244.1.131 ... rack1
-- ============================================================================

-- SCHEMA (run once via any coordinator). NetworkTopologyStrategy with RF=1 in EACH DC.
-- RF=1/DC is sufficient; the bug mechanism is RF-independent (the report's 6-node 3:3 RF=3
-- ring is NOT required).
CREATE KEYSPACE repro16334_ks WITH replication = {'class':'NetworkTopologyStrategy','dc1':1,'dc2':1};
CREATE TABLE repro16334_ks.t (key int PRIMARY KEY, val blob);

-- ============================================================================
-- TRIGGER — the money insert. Run from a CQL FILE (cqlsh -f /tmp/ins.cql) to avoid ARG_MAX
-- with the ~2.2 MB inline hex literal, against a coordinator in either DC (e.g. cass-dc1):
--   kubectl exec -n <ns> cass-dc1 -- cqlsh -f /tmp/ins.cql
--
-- /tmp/ins.cql contents (CONSISTENCY LOCAL_ONE is MANDATORY — cqlsh defaults to ONE, which
-- routes through the all-DC WriteResponseHandler failure count and would MASK the bug):
-- ----------------------------------------------------------------------------
CONSISTENCY LOCAL_ONE;
INSERT INTO repro16334_ks.t (key, val) VALUES (1, 0x<2,200,000 hex chars = 'ab' repeated 1,100,000 times = 1.1 MB byte blob>);
-- ----------------------------------------------------------------------------
-- The 1.1 MB blob (2.2 MB hex) exceeds max_mutation_size_in_kb=1000 (~1 MB), so EVERY
-- replica rejects the mutation.

-- ============================================================================
-- EXPECTED RESULTS (identical workload, only the keyspace topology / build changes):
--
--   BUGGY  4.0.1, multi-DC keyspace (dc1:1, dc2:1)  ==>  WRONG: WriteTimeout (code=1100):
--     Consistency level set to LOCAL_ONE.
--     /tmp/ins.cql:3:WriteTimeout: Error from server: code=1100 [Coordinator node timed out
--       waiting for replica nodes' responses] message="Operation timed out - received only 0
--       responses." info={'consistency': 'LOCAL_ONE', 'required_responses': 1,
--       'received_responses': 0, 'write_type': 'SIMPLE'}
--     command terminated with exit code 2
--
--   BUGGY  4.0.1, single-DC keyspace (dc1:1)        ==>  CORRECT: WriteFailure (code=1500):
--     code=1500 [Replica(s) failed to execute write] ... 'failures': 1 ...   (the contrast)
--
--   FIXED  4.0.2, multi-DC keyspace (dc1:1, dc2:1)  ==>  CORRECT: WriteFailure (code=1500):
--     code=1500 [Replica(s) failed to execute write] ... 'failures': 1 ...   (the A/B control)
--
-- The only variable that flips correct<->buggy is multi-DC on the unfixed 4.0.1 code.
-- ============================================================================
"""

    # Deliberately NOT set (stub): continuous_reproducer stays False and expected_output
    # stays None. The reproducer above is not runnable as single-cluster CQL (it needs a real
    # 2-datacenter ring), so attaching a looping reproducer pod / mitigation oracle would
    # falsely report "reproduced" on the single-DC cluster the standard deploy provides. This
    # stub contributes only the diagnosis oracle (root cause) until a multi-DC harness exists.
    continuous_reproducer = False
    # NOTE: this is an error bug — the WRONG EXCEPTION (WriteTimeout code=1100 instead of
    # WriteFailure code=1500), NOT a wrong-result-that-persists-a-value bug — so expected_output
    # is intentionally unset (no incorrect value is returned/persisted; the write fails either
    # way, only the surfaced error code is wrong).
