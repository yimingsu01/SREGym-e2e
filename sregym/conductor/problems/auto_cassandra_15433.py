"""STUB: multi-node reproduction not yet encoded as a single-cluster Problem — see steps below.

CASSANDRA-15433: Pending ranges are not recalculated on keyspace creation.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-15433
Buggy: 4.0.1   ->   Fixed: 4.0.2 (also 3.0.26, 3.11.12, 4.1-alpha1, 4.1)
Component: Cluster/Membership

Reproduction summary (a MULTI-NODE RING scenario — NOT a single fresh node / single CQL):
A 2-node ring (both NORMAL/UN) plus a third node that is held in BOOT/bootstrap mode
(observed as UJ = Up/Joining in `nodetool status`, pinned in the pending-range window via
`-Dcassandra.ring_delay_ms=600000`). While that node is in BOOT, a keyspace `ks15433`
(SimpleStrategy RF=2) + table + 50 INSERTs are issued through a NORMAL coordinator (cass-0).
Because the keyspace did NOT exist when the joining node's BOOT state change was observed,
pending ranges are not recalculated on its creation, so the joining node is excluded from
all writes for that keyspace. The coordinator's own `SELECT count(*)` returns the correct
50 (the bug is invisible from CQL); the loss is only visible as a `nodetool cfstats` metric
read ON the joining node, where it has received zero of the RF=2 writes.

WHY THIS IS A STUB (do not flatten into one CQL / one fixed-image sequence):
The GenericCustomBuildProblem lifecycle deploys exactly one db_version (4.0.1) as a single
cluster and runs the `reproducer` as a CQL string against it. This bug fundamentally needs
THREE coordinated roles that a CQL string cannot express: (1) two NORMAL ring members, (2) a
THIRD node parked in BOOT/UJ for the duration of the writes, and (3) the writes issued through
one of the NORMAL nodes while the third is still UJ. The signature is not a CQL result at all —
it is a per-node `nodetool cfstats` metric (`Local write count: 0`) read on the specific joining
node. There is no `reproducer` CQL you can run against a deployed cluster that fires or observes
this fault, so the full multi-node steps are transcribed below and `continuous_reproducer` is
left False (no working single-cluster looping reproducer pod). See the authoritative evidence log:
.claude/repro-evidence/repro-CASSANDRA-15433.md

Verbatim buggy signature (from the reproduction evidence log):
  Local write count: 0
(on the joiner's ks15433.t, after 50 RF=2 writes through a NORMAL coordinator while the joiner
is in BOOT/UJ mode; equivalently `Write Count: 0` at the keyspace level). A/B control: the same
workload on fixed 4.0.2 routes writes to the pending replica, giving `Local write count: 36`.
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra15433(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.0.1"
    source_git_ref = "cassandra-4.0.1"
    # 4.0.1 already ships the bug (fix is 4.0.2), so deploy the stock image instead of
    # running a ~30-min `ant jar` source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/schema/Schema.java"
    root_cause_description = (
        "Pending ranges are not recalculated on keyspace creation. When a node begins "
        "bootstrapping, Cassandra recalculates pending token ranges for each keyspace that "
        "EXISTS at the moment the BOOT/BOOT_REPLACE state change is observed "
        "(StorageService.handleState* -> PendingRangeCalculatorService). When a keyspace is "
        "CREATED *after* that, while a node is still in BOOT, the schema-merge path "
        "(Schema.merge, on CREATE KEYSPACE) does NOT trigger a pending-range recalculation for "
        "the joining node. As a result writes for the newly created keyspace are not routed to "
        "the joining node as a pending replica, so once bootstrap completes the joined node is "
        "silently missing all data written to that keyspace during the BOOT window. The fix "
        "(4.0.2) recalculates pending ranges when a keyspace is created so the joining node "
        "receives writes for its pending ranges."
    )

    # STUB: this is a MULTI-NODE RING reproducer. It deliberately needs two NORMAL ring members
    # plus a THIRD node held in BOOT (UJ) while writes are issued through a NORMAL coordinator,
    # and the signature is a per-node `nodetool cfstats` metric read on the joining node. It
    # CANNOT be expressed as a single deployed image + single CQL string. The full phases from
    # the evidence log are recorded here; do NOT collapse this into a single CQL block (it would
    # compile and register but silently fail to reproduce — or even observe — the bug).
    reproducer = """
-- ============================================================================
-- STUB / TODO: multi-node ring reproduction — NOT a single CQL, NOT runnable
-- through the single-image deploy->inject->reproduce lifecycle.
--
-- Requires THREE coordinated roles on one single-DC ring (cluster_name=repro):
--   * cass-0, cass-1 : two NORMAL members (UN), e.g. a StatefulSet replicas=2.
--   * joiner         : a THIRD node parked in BOOT/bootstrap (UJ = Up/Joining),
--                      run as a BARE pod (NOT in the StatefulSet — its CQL
--                      transport is DOWN during bootstrap so a cqlsh readiness
--                      probe would never pass). Held in the pending-range window
--                      via JVM_EXTRA_OPTS=-Dcassandra.ring_delay_ms=600000, which
--                      announces BOOT to gossip then sleeps 600s
--                      ("JOINING: sleeping 600000 ms for pending range setup").
-- All of steps 2-4 happen INSIDE that 600s window, while the joiner is still UJ.
-- ============================================================================

-- PHASE 1 — bring up the 2-node ring; confirm both NORMAL before adding the joiner.
-- shell: kubectl exec cass-0 -- nodetool status   ->   2x UN
-- Then start the `joiner` bare pod with ring_delay_ms=600000 and wait until the
-- coordinator sees it as UJ:
-- shell: kubectl exec cass-0 -- nodetool status
--   UN  <cass-0 ip>   ... rack1
--   UN  <cass-1 ip>   ... rack1
--   UJ  <joiner ip>   ...   ?   ... rack1     <-- joiner = Up/Joining = BOOT mode
-- (The keyspace ks15433 must NOT exist yet at this point — that is the trigger:
--  it is created AFTER the joiner's BOOT state change is observed.)

-- PHASE 2 — while the joiner is still UJ, CREATE the keyspace (RF=2) + table via
--           the NORMAL coordinator cass-0:
-- shell: kubectl exec cass-0 -- cqlsh -e "<the two statements below>"
CREATE KEYSPACE IF NOT EXISTS ks15433 WITH replication = {'class':'SimpleStrategy','replication_factor':2};
CREATE TABLE IF NOT EXISTS ks15433.t (id int PRIMARY KEY, v text);

-- PHASE 3 — while the joiner is STILL UJ, issue 50 RF=2 writes through cass-0
--           (id = 1..50), then sanity-check the cluster-wide count:
-- shell: for id in 1..50: kubectl exec cass-0 -- cqlsh -e \\
--          "INSERT INTO ks15433.t (id, v) VALUES (<id>, 'v<id>');"
INSERT INTO ks15433.t (id, v) VALUES (1, 'v1');
-- ... INSERTs id=2..49 ...
INSERT INTO ks15433.t (id, v) VALUES (50, 'v50');
SELECT count(*) AS n FROM ks15433.t;
--   n
--  ----
--   50          <-- CQL reports the CORRECT count; the bug is INVISIBLE from CQL.
-- (Re-confirm the joiner is still UJ at observation time: nodetool status on cass-0.)

-- PHASE 4 — TRIGGER/OBSERVE the bug ON THE JOINER (the node held in BOOT). The
--           signature is a per-node nodetool metric, not a CQL result:
-- shell: kubectl exec joiner -- nodetool netstats        ->  Mode: JOINING (still BOOT)
-- shell: kubectl exec joiner -- nodetool cfstats ks15433.t
--   Keyspace : ks15433
--       Read Count: 0
--       Write Count: 0                 <-- keyspace-level: zero writes received
--           Table: t
--           ...
--           Local write count: 0       <-- BUGGY SIGNATURE: joiner got 0 of 50 RF=2 writes
--
-- BUGGY (4.0.1): the keyspace created during BOOT did NOT trigger a pending-range
--                recalculation, so the joining node was excluded from all writes
--                -> Local write count: 0 (silent data loss after bootstrap).
-- FIXED  (4.0.2): identical workload -> the joiner receives writes for its pending
--                ranges -> Local write count: 36.
"""

    # STUB: no working single-cluster looping reproducer pod. The single-image
    # deploy->inject->reproduce lifecycle cannot stand up a third node in BOOT (UJ),
    # write through a separate NORMAL coordinator, and read `nodetool cfstats` on the
    # joiner. Left False so a non-functional continuous reproducer is not falsely
    # advertised (a CQL-loop pod running the prose above would be a no-op / false pass).
    continuous_reproducer = False
    # NOTE: this is NOT a wrong-result CQL bug, so expected_output is intentionally unset.
    # The buggy value `0` (Local write count) is a per-node `nodetool cfstats` METRIC read
    # on the joining node, not a value returned or persisted by any CQL query — the
    # coordinator's own SELECT count(*) returns the correct 50. expected_output only feeds
    # the continuous mitigation oracle (not armed here), so setting it would be misleading.
