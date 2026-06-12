"""CASSANDRA-16796: Clear pending ranges for a SHUTDOWN peer.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16796
Buggy: 4.0.0   ->   Fixed: 4.0.1 (also 3.0.25, 3.11.11).
Component: Cluster/Membership

STUB: multi-node reproduction not yet encoded as a single-cluster Problem — see steps below.

Reproduction summary (real 3-node single-token ring; NOT expressible as one CQL string):
A node (cass-2) involved in a `nodetool move` is gracefully shut down WHILE it is still MOVING.
The graceful shutdown announces gossip STATUS shutdown,true, but in 4.0.0 Gossiper.markAsShutdown()
omits the subscriber.onChange(STATUS, shutdown) notification, so TokenMetadata never clears cass-2's
MOVING status. Peers therefore keep cass-2 as Down+Moving (DM) with phantom pending ranges, which can
inflate a coordinator's required-replica count and produce bogus UnavailableException responses to
clients. The fix commit fbb20b9162b73c4de8a82cf4ffdde3304e904603 adds the missing
subscriber.onChange(endpoint, ApplicationState.STATUS, shutdown) call so TokenMetadata clears the
MOVING status / pending ranges on shutdown.

Verbatim buggy signature (peer cass-0 observing the SHUTDOWN of cass-2, cassandra:4.0.0):

  --- DETERMINISTIC, PERSISTENT signature (the root cause) -------------------------------------------
  cass-2 announced a graceful SHUTDOWN (not a hard crash), per cass-0 `nodetool gossipinfo`:
      /10.244.2.132
        generation:1781256306
        heartbeat:2147483647
        STATUS_WITH_PORT:410:shutdown,true
  BUT cass-0's TokenMetadata still shows cass-2 as MOVING -> `DM` (Down + Moving),
  `kubectl exec -n repro-16796 cass-0 -- nodetool status repro16796`:
      --  Address       Load       Tokens  Owns (effective)  Host ID                               Rack
      UN  10.244.3.126  96.13 KiB  1       60.8%             6569297b-4682-481e-a4f6-a7161d41b6a1  rack1
      DM  10.244.2.132  91.16 KiB  1       76.2%             dd8c2e07-0639-4720-bdcd-964442c81f72  rack1
      UN  10.244.1.156  96.22 KiB  1       63.1%             cfa16fe3-43c0-4c3c-96a6-474801436346  rack1
  `DM` after `shutdown,true` IS the buggy state: graceful shutdown replaced MOVING with SHUTDOWN in
  gossip but did NOT clear MOVING from TokenMetadata. A FIXED build (>=4.0.1) fires onChange and the
  node reads `DN` (Down/Normal) with pending ranges cleared. cass-0 system.log only ever marks cass-2
  DOWN, never "removed"/"state normal", so the moving status sticks:
      INFO  [GossipStage:1] 2026-06-12 09:27:52,603 Gossiper.java:1286 - InetAddress /10.244.2.132:7000 is now DOWN

  --- DOWNSTREAM client symptom (intermittent / RACE-Y — the Jira's named symptom) ------------------
  With cass-2 stuck `DM` (shutdown but still MOVING) and BOTH peers `UN`, a QUORUM write to a key
  whose natural replicas are {cass-0 (UP), cass-2 (DOWN)} can fail with a BOGUS UnavailableException —
  the coordinator added a phantom PENDING replica (cass-1), inflating required_replicas 2 -> 3, which
  is impossible under RF=2:
      <stdin>:1:NoHostAvailable: ('Unable to complete the operation against any hosts',
        {<Host: 127.0.0.1:9042 dc1>: Unavailable('Error from server: code=1000 [Unavailable exception]
        message="Cannot achieve consistency level QUORUM"
        info={'consistency': 'QUORUM', 'required_replicas': 3, 'alive_replicas': 2}')})
  Per the Jira ("peers can *sometimes* maintain pending ranges"), this client symptom is intermittent:
  an intervening gossip event can trigger a PendingRangeCalculator rerun that recomputes the bad range
  mapping away. The PERSISTENT, deterministic part of the bug is the `DM` TokenMetadata state above.

WHY THIS IS A STUB (do not flatten into one CQL):
The GenericCustomBuildProblem lifecycle deploys ONE db_version and runs the reproducer against that
single deployed image. This bug needs a 3-node single-token ring where one node is mid-`nodetool move`
and is then gracefully shut down, and where the buggy state (`DM` / phantom pending ranges) is observed
on a DIFFERENT peer's TokenMetadata. None of that — the ring formation, the background move, the poll
for the MOVING window, the graceful scale-down mid-move, or the per-peer divergence — can be expressed
by a single `reproducer` CQL string. The full multi-node steps from the evidence log are recorded below
in `reproducer` and `continuous_reproducer` is left False (no working single-cluster looping reproducer
pod). See the authoritative evidence log: .claude/repro-evidence/repro-CASSANDRA-16796.md
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra16796(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.0.0"
    source_git_ref = "cassandra-4.0.0"
    # 4.0.0 already ships the bug (fix is 4.0.1), so deploy the stock image
    # instead of running a ~30-min `ant jar` source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/gms/Gossiper.java"
    root_cause_description = (
        "On a single-token ring, a node (cass-2) that is mid-`nodetool move` and is then gracefully "
        "shut down leaves its peers holding phantom pending ranges for it. The graceful shutdown "
        "announces gossip STATUS shutdown,true, but in 4.0.0 Gossiper.markAsShutdown() sets the local "
        "SHUTDOWN state WITHOUT calling subscriber.onChange(endpoint, ApplicationState.STATUS, "
        "shutdown). Because that notification is missing, TokenMetadata is never told the node left and "
        "never clears its MOVING status, so peers keep the node as Down+Moving (DM) with stale pending "
        "ranges. A coordinator can then add a phantom pending replica to a write set, inflating the "
        "required-replica count beyond RF and returning bogus UnavailableException responses to clients. "
        "The fix (4.0.1, commit fbb20b9162b73c4de8a82cf4ffdde3304e904603) adds the missing "
        "subscriber.onChange(endpoint, ApplicationState.STATUS, shutdown) call in markAsShutdown() so "
        "TokenMetadata clears the MOVING status and pending ranges on shutdown."
    )

    # STUB reproducer — multi-node single-token ring steps from the evidence log (NOT a single
    # runnable CQL). ORDERING IS LOAD-BEARING. Flattening this into one CQL block would compile and
    # register but silently NOT reproduce the bug (the move + graceful-shutdown-mid-move across peers
    # cannot be expressed as CQL against a single deployed image).
    reproducer = """
-- ============================================================================
-- STUB / TODO: multi-node reproduction — NOT a single CQL block.
-- Requires a 3-node single-token ring (num_tokens=1) on cassandra:4.0.0, as a
-- StatefulSet `cass` (cass-0/1/2) with ephemeral storage and
-- terminationGracePeriodSeconds=90 so a SIGTERM drains and ANNOUNCES the gossip
-- SHUTDOWN state (shutdown,true) instead of being SIGKILLed. With the Docker
-- image's default vnodes (16 tokens) `nodetool move` is rejected
-- ("This node has more than one token and cannot be moved thusly."), so the
-- ring MUST be single-token. ORDERING IS LOAD-BEARING.
-- ============================================================================

-- 1. Form a 3-node single-token ring on cassandra:4.0.0 (StatefulSet cass-0/1/2).
--    Fixed token assignment:
--      cass-2 = -3584644331145400280   (highest-ownership node)
--      cass-1 =   813234791936175363
--      cass-0 =  8051695314435402860

-- 2. Create the keyspace + table and seed 30 rows:
CREATE KEYSPACE repro16796 WITH REPLICATION = {'class': 'SimpleStrategy', 'replication_factor': 2};
CREATE TABLE repro16796.t (id int PRIMARY KEY, v text);
-- shell: for k in 1..30: INSERT INTO repro16796.t (id, v) VALUES (<k>, 'init');

-- 3. Widen the MOVING window by throttling streaming on ALL nodes (run on each):
-- shell: nodetool setstreamthroughput 1

-- 4. On cass-2 (highest-ownership node), start a move IN THE BACKGROUND. The
--    target token 3000000000000000000 lies between cass-1 (8.13e17) and
--    cass-0 (8.05e18), so cass-2 becomes a PENDING replica for the range
--    (813234791936175363, 3000000000000000000] and peers begin holding pending
--    ranges for cass-2:
-- shell (on cass-2, backgrounded): nodetool move 3000000000000000000

-- 5. Poll `nodetool status` until cass-2 shows `UM` (Up/Moving). Peers now hold
--    pending ranges for cass-2:
-- shell: nodetool status   ==> wait for the cass-2 row to read `UM`

-- 6. While cass-2 is still MOVING, gracefully shut it down (SIGTERM -> drain ->
--    gossip SHUTDOWN). This is the trigger:
-- shell: kubectl scale statefulset cass -n repro-16796 --replicas=2

-- 7. OBSERVE THE BUG on a surviving peer (e.g. cass-0):
--    (a) cass-2 announced a graceful SHUTDOWN, per cass-0 `nodetool gossipinfo`:
--          /10.244.2.132
--            STATUS_WITH_PORT:410:shutdown,true
--    (b) BUT cass-0 still shows cass-2 as MOVING -> `DM` (Down + Moving):
-- shell: kubectl exec -n repro-16796 cass-0 -- nodetool status repro16796
--          DM  10.244.2.132  91.16 KiB  1  76.2%  dd8c2e07-...  rack1
--    `DM` after `shutdown,true` IS the buggy state (a FIXED build >=4.0.1 reads
--    `DN`, with pending ranges cleared). This DM TokenMetadata state is the
--    PERSISTENT, deterministic signature of CASSANDRA-16796.

-- 8. (DOWNSTREAM, INTERMITTENT client symptom — the Jira's named symptom)
--    With cass-2 stuck `DM` and both peers `UN`, a QUORUM write to a key whose
--    natural replicas are {cass-0 (UP), cass-2 (DOWN)} can fail with a BOGUS
--    UnavailableException because the coordinator adds a phantom PENDING replica
--    (cass-1), inflating required_replicas 2 -> 3 (impossible under RF=2):
-- shell (coordinated from cass-0): CONSISTENCY QUORUM;
--        INSERT INTO repro16796.t (id, v) VALUES (21, 'sweep');
--   ==> Unavailable: required_replicas: 3, alive_replicas: 2   (BOGUS for RF=2)
--    This symptom is RACE-Y: an intervening gossip event can trigger a
--    PendingRangeCalculator rerun that recomputes the bad mapping away. The
--    deterministic part of the bug is the DM state in step 7.
"""

    # STUB: no working single-cluster looping reproducer pod. The multi-node move +
    # graceful-shutdown-mid-move sequence (and the per-peer DM divergence) cannot run inside the
    # single-image deploy->inject->reproduce lifecycle, so continuous_reproducer is left False.
    # Setting it True would attach a ReproducerPodMitigationOracle to a looping pod that can never
    # reproduce this multi-node bug (the anti-pattern the skill explicitly warns against).
    continuous_reproducer = False
    # Error/availability bug, NOT a wrong-result bug: the buggy state surfaces as a stale `DM`
    # TokenMetadata entry and a bogus UnavailableException (an error), not as a returned/persisted
    # wrong value, so expected_output is intentionally NOT set.
