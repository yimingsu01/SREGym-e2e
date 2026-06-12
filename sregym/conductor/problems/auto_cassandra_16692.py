"""STUB: multi-node reproduction not yet encoded as a single-cluster Problem — see steps below.

CASSANDRA-16692: Unable to replace node with stale schema.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16692
Buggy: 3.11.10   ->   Fixed: 3.11.11 (also 3.0.25, 4.0-rc1)
Component: Cluster/Schema

Reproduction summary (a MULTI-NODE RING scenario — NOT a single fresh node / single CQL):
In a Cassandra ring, shut down one node, then CREATE a new keyspace/table on a surviving
node (bumping the cluster schema version to V1) while the dead node still carries the
old/stale schema (V0) in gossip. Then replace the terminated node with a fresh non-seed
node booted with -Dcassandra.replace_address_first_boot=<deadNodeIP>. On the buggy 3.11.10
image the replacement node's startup waits for schema agreement across ALL known endpoints —
including the dead node it is replacing, whose stale schema V0 can never reconcile against the
live seed's V1 — so it blocks in JOINING ("waiting for ring information") and dies on a
schema-agreement timeout before it can join the ring. Minimal faithful repro = a 2-node ring
(1 seed + 1 victim) plus 1 replacement pod.

WHY THIS IS A STUB (do not flatten into one CQL / one fixed-image sequence):
The GenericCustomBuildProblem lifecycle deploys exactly one db_version (3.11.10) as a single
cluster and runs the `reproducer` as a CQL string against it. This bug fundamentally needs
THREE coordinated roles that a CQL string cannot express: (1) a NORMAL seed node, (2) a SECOND
"victim" node that is brought up to form the ring and then DELETED (not decommissioned) so its
stale schema persists in the seed's gossip, and (3) a THIRD "replacement" node booted as a
NON-seed with -Dcassandra.replace_address_first_boot=<victim IP>. It is ALSO not a
crash_on_startup config-gated bug: a single fresh node has no down-peer-carrying-stale-schema to
block on, so the single-image deploy->swap-buggy->wait-for-CrashLoopBackOff lifecycle CANNOT
reproduce it. The failure is a JVM startup RuntimeException on the replacement pod (pod Failed,
container exit code 3), not a CQL result. There is no `reproducer` CQL you can run against a
deployed cluster that fires this fault, so the full multi-node steps are transcribed below and
`continuous_reproducer` is left False (no working single-cluster looping reproducer pod). See the
authoritative evidence log: .claude/repro-evidence/repro-CASSANDRA-16692.md

Verbatim buggy signature (from the reproduction evidence log; replacement pod, cassandra:3.11.10):
  ERROR [main] CassandraDaemon.java:803 - Exception encountered during startup
  java.lang.RuntimeException: Didn't receive schemas for all known versions within the timeout
        at org.apache.cassandra.service.StorageService.waitForSchema(StorageService.java:947)
        at org.apache.cassandra.service.StorageService.joinTokenRing(StorageService.java:987)
        at org.apache.cassandra.service.StorageService.initServer(StorageService.java:753)
        at org.apache.cassandra.service.StorageService.initServer(StorageService.java:687)
        at org.apache.cassandra.service.CassandraDaemon.setup(CassandraDaemon.java:395)
        at org.apache.cassandra.service.CassandraDaemon.activate(CassandraDaemon.java:633)
        at org.apache.cassandra.service.CassandraDaemon.main(CassandraDaemon.java:786)
(Replacement pod exited phase=Failed, container exitCode=3.) A/B control on fixed 3.11.11: the
identical sequence has ZERO occurrences of this message — the replaced/down node is exempted from
the schema-agreement wait, so the replacement bootstraps and joins the ring (UN).
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra16692(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "3.11.10"
    source_git_ref = "cassandra-3.11.10"
    # 3.11.10 already ships the bug (fix is 3.11.11), so deploy the stock image instead of
    # running a ~30-min `ant jar` source build.
    prebuilt_from_stock = True

    # Root-cause file as evidenced by the startup stack trace in the reproduction log:
    # waitForSchema (line 947) -> joinTokenRing (line 987) -> initServer all live in
    # StorageService.java. CASSANDRA-15158 introduced the all-endpoints schema-agreement
    # wait here; CASSANDRA-16692 is the fix that exempts the replaced node.
    root_cause_file = "src/java/org/apache/cassandra/service/StorageService.java"
    root_cause_description = (
        "Unable to replace a terminated node when a stale schema lingers in gossip. After "
        "CASSANDRA-15158, StorageService.joinTokenRing calls waitForSchema "
        "(StorageService.java:947), which blocks startup until it receives schema for ALL known "
        "endpoints — including the down node being replaced. The dead node's stale schema "
        "version (V0) can never reconcile against the live seed's newer version (V1, bumped by a "
        "CREATE KEYSPACE/TABLE issued while the victim was down), so on the buggy 3.11.10 image "
        "the replacement node sits in JOINING ('waiting for ring information') and dies with "
        "RuntimeException: 'Didn't receive schemas for all known versions within the timeout' "
        "before it can join the ring. Buggy 3.11.10 has CASSANDRA-15158 but not the "
        "CASSANDRA-16692 fix, which exempts the replaced node from the schema-agreement wait."
    )

    # STUB: this is a MULTI-NODE RING reproducer. It deliberately needs a NORMAL seed, a SECOND
    # node brought up then DELETED to leave a stale schema in gossip, and a THIRD non-seed
    # replacement node booted with -Dcassandra.replace_address_first_boot=<victim IP>. The
    # signature is a JVM startup RuntimeException on the replacement pod, not a CQL result. It
    # CANNOT be expressed as a single deployed image + single CQL string. The full phases from the
    # evidence log are recorded here; do NOT collapse this into a single CQL block, and do NOT set
    # crash_on_startup (a single fresh node has no stale-schema down-peer to block on) — either
    # would compile and register but silently fail to reproduce the bug.
    reproducer = """
-- ============================================================================
-- STUB / TODO: multi-node ring node-replacement reproduction — NOT a single CQL,
-- NOT runnable through the single-image deploy->inject->reproduce lifecycle, and
-- NOT a crash_on_startup config-gated bug (a single fresh node cannot reproduce).
--
-- Requires THREE coordinated roles on one single-DC ring (cluster_name=repro16692),
-- run as STANDALONE pods (not a StatefulSet) for per-pod replace control:
--   * seed        : NORMAL member (UN). Note its pod IP -> SEED_IP.
--   * victim      : a SECOND member booted with CASSANDRA_SEEDS=<SEED_IP> to form a
--                   2-node ring, then DELETED (see PHASE 2 — NOT decommissioned).
--   * replacement : a THIRD pod, fresh ephemeral data, booted as a NON-seed with
--                   CASSANDRA_SEEDS=<SEED_IP> and
--                   JVM_EXTRA_OPTS=-Dcassandra.replace_address_first_boot=<VICTIM_IP>.
--                   (A seed would skip bootstrap and the schema wait; a NON-seed is required.)
-- All of PHASE 2..4 happen with the victim DOWN-but-still-in-gossip.
-- ============================================================================

-- PHASE 1 — bring up the 2-node ring; confirm BOTH NORMAL and on a SINGLE schema
--           version before doing anything else.
-- shell: start `seed` (cassandra:3.11.10); record SEED_IP (e.g. 10.244.2.112).
-- shell: start `victim` (cassandra:3.11.10) with CASSANDRA_SEEDS=<SEED_IP>; record VICTIM_IP.
-- shell: kubectl exec seed -- nodetool status
--   UN  <SEED_IP>     ... rack1
--   UN  <VICTIM_IP>   ... rack1     <-- 2x UN, single schema version V0 (e.g. e84b6a60-...).

-- PHASE 2 — take the victim DOWN, but KEEP its gossip state (this is the crux):
-- shell: kubectl delete pod victim        <-- DELETE the pod.
--   ** Do NOT `nodetool decommission` and do NOT `nodetool removenode` — those purge the
--      victim's gossip entry and the stale schema VANISHES, which kills the reproduction. **
-- shell: kubectl exec seed -- nodetool status   ->  victim is now DN (Down/Normal).
-- shell: kubectl exec seed -- nodetool gossipinfo   ->  the victim's gossip entry PERSISTS:
--   /<VICTIM_IP>   STATUS: shutdown,true   SCHEMA: <V0>   (the stale schema, still advertised).

-- PHASE 3 — while the victim is DOWN, bump the cluster schema on the SURVIVING seed so the
--           ring diverges into two schema versions (V0 on the dead victim, V1 on the seed):
-- shell: kubectl exec seed -- cqlsh -e "<the two statements below>"
CREATE KEYSPACE IF NOT EXISTS repro16692_ks WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE IF NOT EXISTS repro16692_ks.t (id int PRIMARY KEY, v text);
-- (Side note from the log: running this DDL while the peer is DOWN may surface cqlsh
--  NoHostAvailable / "schema version mismatch detected" warnings, but the DDL still applies
--  locally and the seed's schema version bumps to V1 — this is expected, not the bug.)
-- shell: kubectl exec seed -- nodetool gossipinfo   ->  confirm the V0/V1 DIVERGENCE:
--   /<VICTIM_IP> (dead victim):  SCHEMA: <V0>   (stale; never reconcilable)
--   /<SEED_IP>   (live seed):    SCHEMA: <V1>   (e.g. 9720161e-...).

-- PHASE 4 — TRIGGER the bug: replace the terminated victim with a fresh NON-seed node.
-- shell: start `replacement` (cassandra:3.11.10), fresh ephemeral data,
--        CASSANDRA_SEEDS=<SEED_IP>,
--        JVM_EXTRA_OPTS=-Dcassandra.replace_address_first_boot=<VICTIM_IP>.
-- Replacement startup log shows the replace flag took, then it parks in JOINING and times out:
--   INFO  [main] CassandraDaemon.java:507 - JVM Arguments: [... -Dcassandra.replace_address_first_boot=<VICTIM_IP> ...]
--   INFO  [main] StorageService.java:1536 - JOINING: waiting for ring information
--   ERROR [main] CassandraDaemon.java:803 - Exception encountered during startup
--   java.lang.RuntimeException: Didn't receive schemas for all known versions within the timeout
--         at org.apache.cassandra.service.StorageService.waitForSchema(StorageService.java:947)
--         at org.apache.cassandra.service.StorageService.joinTokenRing(StorageService.java:987)
--         at org.apache.cassandra.service.StorageService.initServer(StorageService.java:753)
--         at org.apache.cassandra.service.StorageService.initServer(StorageService.java:687)
--         at org.apache.cassandra.service.CassandraDaemon.setup(CassandraDaemon.java:395)
--         at org.apache.cassandra.service.CassandraDaemon.activate(CassandraDaemon.java:633)
--         at org.apache.cassandra.service.CassandraDaemon.main(CassandraDaemon.java:786)
--   (replacement pod -> phase=Failed, container exitCode=3.)
--
-- BUGGY (3.11.10): replacement blocks on schema agreement with the dead victim (stale V0,
--                  never reconcilable) and dies on a timeout -> the node never joins the ring.
-- FIXED  (3.11.11): identical sequence -> the replaced/down node is EXEMPTED from the
--                  schema-agreement wait ("JOINING: schema complete, ready to bootstrap"),
--                  so the replacement bootstraps and joins the ring (UN).
"""

    # STUB: no working single-cluster looping reproducer pod. The single-image
    # deploy->inject->reproduce lifecycle cannot stand up a seed, a deleted-but-still-gossiped
    # victim, and a non-seed replacement booted with -Dcassandra.replace_address_first_boot.
    # Left False so a non-functional continuous reproducer is not falsely advertised (a CQL-loop
    # pod running the prose above would be a no-op / false pass).
    continuous_reproducer = False
    # NOTE: this is NOT a wrong-result CQL bug, so expected_output is intentionally unset.
    # The signature is a JVM startup RuntimeException on the replacement pod (a crash/error,
    # not a value returned or persisted by any CQL query). expected_output only feeds the
    # continuous mitigation oracle (not armed here), so setting it would be misleading.
