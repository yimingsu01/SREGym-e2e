"""CASSANDRA-20877: FINALIZED incremental repair sessions are never cleaned up after range movement.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-20877
Buggy version: 4.0.19  ->  Fixed version: 4.0.20

STUB: multi-node reproduction not yet encoded as a single-cluster Problem — see steps below.

Reproduction summary
---------------------
`system.repairs` is local per node and pruned by `LocalSessions#cleanup()` every
`cassandra.repair_cleanup_interval_seconds`; it deletes FINALIZED sessions older than
`cassandra.repair_delete_timeout_seconds` ONLY if `LocalSessions#isSuperseded(session)` is true
(every range+table the session covered has since been re-repaired by a newer session). After a node
bootstraps/decommissions, a set of ranges moves off the old nodes; those moved ranges are no longer
re-repaired on the old nodes, so the last pre-movement session is never superseded and its FINALIZED
row is kept forever. Reproduced on a 3-node ring (RF=2) bootstrapped from an initial 2-node ring: repair
S1 (2-node ring) -> scale to 3 nodes (range movement) -> repair S2 (coordinated on cass-0) -> wait past
the delete timeout. On the buggy build (4.0.19) the coordinator node cass-0 retains the pre-movement
session S1 (state=4 FINALIZED) indefinitely; on the fix (4.0.20) S1 is auto-deleted on cass-0.

This bug REQUIRES multi-pod orchestration (kubectl scale to bootstrap a new node = range movement, two
nodetool repairs across the topology change, per-node divergence where cass-0 is the discriminator, and
a recurring debug.log signature rather than a CQL response). It CANNOT be expressed as a single
`reproducer` CQL string, so it is intentionally left as an honest stub (continuous_reproducer = False).

Verbatim buggy signature (4.0.19 / cass-0 debug.log — recurs every cleanup interval, forever):
    DEBUG [OptionalTasks:1] 2026-06-11 21:36:32,411 LocalSessions.java:456 - Skipping delete of FINALIZED LocalSession ed5be870-65dc-11f1-8c53-6deb776ceda9 because it has not been superseded by a more recent session
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra20877(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.0.19"
    source_git_ref = "cassandra-4.0.19"
    # 4.0.19 already ships the bug (fixed in 4.0.20), so deploy the stock image
    # instead of running a source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/repair/consistent/LocalSessions.java"
    root_cause_description = (
        "After a node bootstraps or decommissions, ranges move off the old nodes and are no longer "
        "re-repaired there, so the last pre-movement FINALIZED incremental-repair session in "
        "system.repairs is never superseded. LocalSessions#cleanup() only auto-deletes a FINALIZED "
        "session when LocalSessions#isSuperseded(session) is true (every range+table it covered has "
        "since been re-repaired by a newer session), so the stale session's row is retained "
        "indefinitely — the cleanup pass logs 'Skipping delete of FINALIZED LocalSession ... because "
        "it has not been superseded by a more recent session' on every interval. The fix (4.0.20) "
        "ignores ranges the node no longer owns when deciding supersession, allowing the moved-range "
        "session to be deleted."
    )

    # STUB: multi-node reproduction not yet encoded as a single-cluster Problem — see steps below.
    # The full reproduction needs a 3-node ring (RF=2) bootstrapped from a 2-node ring to create the
    # range movement; it cannot be flattened into one CQL block. continuous_reproducer is therefore
    # False so this stub does NOT falsely claim a working looping reproducer.
    reproducer = """
-- STUB: multi-node reproduction not yet encoded as a single-cluster Problem.
-- Topology: 3-node StatefulSet, RF=2, bootstrapped FROM an initial 2-node ring to force range movement.
-- Set these JVM props on ALL pods (e.g. via JVM_EXTRA_OPTS) so the 1-day delete timeout / 10-min cleanup
-- interval become testable in budget:
--   -Dcassandra.repair_delete_timeout_seconds=30 -Dcassandra.repair_cleanup_interval_seconds=20
--
-- Step 1. Deploy a 2-node StatefulSet (cass-0, cass-1); wait for 2 UN.
-- Step 2. Create schema, insert data, flush both nodes:
CREATE KEYSPACE ks20877 WITH REPLICATION = {'class': 'SimpleStrategy', 'replication_factor': 2};
CREATE TABLE ks20877.t (id int PRIMARY KEY, v text);
INSERT INTO ks20877.t (id, v) VALUES (1, 'a');
INSERT INTO ks20877.t (id, v) VALUES (2, 'b');
INSERT INTO ks20877.t (id, v) VALUES (3, 'c');
INSERT INTO ks20877.t (id, v) VALUES (4, 'd');
INSERT INTO ks20877.t (id, v) VALUES (5, 'e');
-- (then: nodetool flush on cass-0 AND cass-1)
--
-- Step 3. S1 = `nodetool repair ks20877` on cass-0 (default = incremental). Expect "Repair command #1 finished".
--   Confirm S1 is FINALIZED (state=4) on BOTH nodes:
SELECT parent_id, state FROM system.repairs;
--
-- Step 4. Range movement: `kubectl scale statefulset/cass --replicas=3` to bootstrap cass-2; wait for 3 UN.
--   Ownership shifts (~100%/100% -> ~64.7%/59.3%/76.0%); cass-0 and cass-1 cede ranges to cass-2.
--
-- Step 5. S2 = `nodetool flush` on all 3 nodes, then `nodetool repair ks20877` on cass-0 again.
--   Expect "Repair command #2 finished". S2 (coordinated on cass-0) advances repairedAt for every range
--   cass-0 STILL replicates — but NOT the ranges that moved to cass-2.
--
-- Step 6. Wait > delete_timeout + 2*cleanup_interval (~90s) so S1 ages well past the 30s delete timeout,
--   then re-query system.repairs on cass-0:
SELECT parent_id, state FROM system.repairs;
--
-- BUGGY (4.0.19) outcome on cass-0: S1 (pre-movement, state=4) SURVIVES alongside S2 (2 rows). cass-0's
--   debug.log recurs every cleanup interval:
--   "LocalSessions.java:456 - Skipping delete of FINALIZED LocalSession <S1> because it has not been
--    superseded by a more recent session".
-- FIXED (4.0.20) outcome on cass-0: S1 is auto-deleted; cass-0 ends with S2 only (1 row).
-- (cass-0 is the discriminator: S2 was coordinated on cass-0, so the only ranges S1 still owns that S2
--  did NOT re-repair are exactly the ones that moved to cass-2. cass-1 retains S1 under both builds and
--  does not discriminate.)
"""
    # Multi-node STUB: not a runnable single-cluster continuous reproducer.
    continuous_reproducer = False
    # No expected_output: this is FINALIZED-session retention + a recurring debug.log line, not a query
    # that returns a wrong scalar value.
