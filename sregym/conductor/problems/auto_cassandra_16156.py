"""CASSANDRA-16156: Decommissioned nodes are picked for gossip when unreachable nodes are considered.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16156

Buggy: 3.11.8  ->  Fixed: 3.11.9 (also 3.0.23, 4.0).
Components: Cluster/Gossip.

STUB: multi-node ring reproduction not yet encoded as a single-cluster Problem — see steps below.

Reproduction summary (2-node ring, gossip/topology bug; a 2-node ring is sufficient and fastest):
  Bring up a 2-node ring on cassandra:3.11.8 (cass-0 = seed/survivor, cass-1 = victim).
  `nodetool decommission` cass-1 so it transitions to LEFT and leaves the ring, then scale
  the StatefulSet to 1 replica so cass-1's pod and its port 7000 disappear (becomes
  unreachable). With DEBUG enabled at runtime on the survivor for
  org.apache.cassandra.net.OutboundTcpConnection and org.apache.cassandra.gms.Gossiper
  (nodetool setlogginglevel, no restart), the survivor KEEPS selecting the departed LEFT
  node for gossip via the unreachable-member path (Gossiper.maybeGossipToUnreachableMember
  -> sendGossip) and repeatedly tries to connect to it, logging connection failures on the
  MessagingService-Outgoing-/<ip>-Gossip thread. The released fix (cassandra:3.11.9) stops
  selecting the LEFT node: under the identical workload there are ZERO further connect
  attempts and ZERO connection failures after the victim is convicted LEFT/DOWN.

Verbatim buggy signature (from the reproduction evidence log; cass-0 /var/log/cassandra/debug.log,
cassandra:3.11.8 — DEBUG, OLD OutboundTcpConnection stack, port 7000, real pod IPs):
  DEBUG [MessagingService-Outgoing-/10.244.2.125-Gossip] 2026-06-12 08:46:00,222 OutboundTcpConnection.java:546 - Unable to connect to /10.244.2.125
  java.net.ConnectException: Connection timed out
  \tat sun.nio.ch.Net.connect0(Native Method) ~[na:1.8.0_272]
  \tat sun.nio.ch.Net.connect(Net.java:482) ~[na:1.8.0_272]
  \tat sun.nio.ch.Net.connect(Net.java:474) ~[na:1.8.0_272]
  \tat sun.nio.ch.SocketChannelImpl.connect(SocketChannelImpl.java:647) ~[na:1.8.0_272]
  \tat org.apache.cassandra.net.OutboundTcpConnectionPool.newSocket(OutboundTcpConnectionPool.java:146) ~[apache-cassandra-3.11.8.jar:3.11.8]
  \tat org.apache.cassandra.net.OutboundTcpConnectionPool.newSocket(OutboundTcpConnectionPool.java:132) ~[apache-cassandra-3.11.8.jar:3.11.8]
  \tat org.apache.cassandra.net.OutboundTcpConnection.connect(OutboundTcpConnection.java:434) [apache-cassandra-3.11.8.jar:3.11.8]
  \tat org.apache.cassandra.net.OutboundTcpConnection.run(OutboundTcpConnection.java:262) [apache-cassandra-3.11.8.jar:3.11.8]
(Sustained: two distinct "Unable to connect to /<ip>" failures ~2 min apart, cadence set by the OS
connect-timeout in kind's blackhole net; each timeout immediately re-triggers the next attempt. The
LEFT node keeps being selected as long as it stays in the gossip endpoint-state map.)

WHY THIS IS A STUB (do not flatten into one CQL block):
The GenericCustomBuildProblem lifecycle deploys exactly ONE Cassandra cluster (a single node by
default) and runs the `reproducer` CQL against it. This bug is purely about cross-node gossip
topology: it needs a real 2-node ring so one node can be DECOMMISSIONED (transition to LEFT) and
then made UNREACHABLE (scale the StatefulSet down so the departed pod's port 7000 disappears), and
the SURVIVING peer is the thing under test — it wrongly keeps selecting that LEFT node for gossip
and tries to connect to it. There is NO CQL in this reproducer at all; the trigger is
nodetool/topology operations across two pods plus runtime DEBUG logging on the survivor. A single
node cannot decommission a peer, cannot be made to gossip to a departed peer, and an inject_fault()
override (nodetool/CQL on one pod) cannot stand up the second ring member or change the deployed
cluster topology. A flattened single-cluster version would compile and register but SILENTLY NOT
reproduce the bug. The full multi-node steps from the evidence log are preserved verbatim in
`reproducer` below so this can be promoted to a real multi-node Problem once a multi-node ring
harness exists. See the authoritative evidence log:
.claude/repro-evidence/repro-CASSANDRA-16156.md
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra16156(GenericCustomBuildProblem):
    # STUB: see module docstring. Multi-node (2-node ring) gossip/topology bug that cannot be
    # reproduced by a single-cluster CQL reproducer (the standard deploy gives a single node, and
    # the bug requires decommissioning a SECOND node and then making it unreachable while the
    # survivor keeps gossiping to it). Fields below are set so the Problem registers and carries
    # the root-cause + full multi-node steps, but `reproducer` is NOT a runnable single-cluster
    # CQL block (continuous_reproducer stays False and no expected_output is set, to avoid arming
    # a mitigation oracle that would falsely report "reproduced" on a single node that physically
    # cannot gossip to a departed peer).
    db_name = "cassandra"
    db_version = "3.11.8"
    source_git_ref = "cassandra-3.11.8"
    # 3.11.8 already ships the bug (buggy = fix patch 3.11.9 - 1), so deploy the stock image
    # instead of running a ~30-min `ant jar` source build.
    prebuilt_from_stock = True

    # Root cause is the gossip SELECTION of a LEFT node, NOT the OutboundTcpConnection logging
    # site that dominates the verbatim stack trace. The Jira body / evidence log identify the
    # defect as Gossiper.maybeGossipToUnreachableMember -> sendGossip picking a departed node.
    root_cause_file = "src/java/org/apache/cassandra/gms/Gossiper.java"
    root_cause_description = (
        "After a node is decommissioned it transitions to LEFT and leaves the ring, but the "
        "surviving peer's Gossiper STILL selects it for gossip through the unreachable-member "
        "path: Gossiper.run()'s periodic GossipTask calls maybeGossipToUnreachableMember(), which "
        "draws an endpoint from the unreachableEndpoints map and calls sendGossip() to it without "
        "excluding endpoints whose STATUS is LEFT (a departed node). Because the LEFT node remains "
        "in the gossip endpoint-state map (normal expiry handling) but its pod / port 7000 is gone, "
        "every selection produces an OutboundTcpConnection connect attempt on the "
        "MessagingService-Outgoing-/<ip>-Gossip thread that fails (java.net.ConnectException), "
        "producing repeated connection-failure log spam targeting the departed node. The fix "
        "(3.11.9 / 3.0.23 / 4.0) stops selecting LEFT/dead-state endpoints for gossip, so after "
        "the node is convicted LEFT/DOWN there are zero further connect attempts. Component: "
        "Cluster/Gossip. NOTE: the verbatim stack trace points at OutboundTcpConnection.java "
        "(connect/newSocket) — that is only the symptom/logging site, not the root cause."
    )

    # Full multi-node reproduction steps (from the evidence log). This is NOT a runnable
    # single-cluster CQL block — it contains NO CQL at all. It requires a 2-node ring where one
    # node is decommissioned (-> LEFT) and then made unreachable (scale StatefulSet to 1), and the
    # SURVIVING peer is observed (DEBUG at runtime) to keep gossip-connecting to the departed node.
    # Encoded here verbatim so a future multi-node harness can execute it; the single-cluster
    # GenericCustomBuildProblem injector cannot run it as-is.
    reproducer = """
-- STUB: 2-node ring, cassandra:3.11.8. Requires a real ring topology (decommission a peer to
-- LEFT, then make it unreachable) plus runtime DEBUG logging on the survivor; CANNOT be flattened
-- into a single-cluster CQL reproducer (there is NO CQL — the trigger is nodetool/topology ops
-- across two pods). The released fix (cassandra:3.11.9) stops the post-LEFT gossip connect
-- attempts under the IDENTICAL workload — see the A/B control in the evidence log.

-- ============================================================================
-- CLUSTER PRECONDITIONS
--   * 2-node StatefulSet `cass`, image cassandra:3.11.8 (ephemeral storage / no PVC OK).
--       cass-0 = seed = SURVIVOR (the node under test)   e.g. 10.244.1.146
--       cass-1 = VICTIM (to be decommissioned + removed)  e.g. 10.244.2.125
--   * Both nodes UN (up/normal) before reproduction (`nodetool status` shows 2x UN).
-- ============================================================================

-- STEP 1 — bring up the 2-node ring and confirm both UN:
--   shell: kubectl apply -f <2-node StatefulSet, cassandra:3.11.8>
--   shell: kubectl rollout status statefulset/cass --timeout=900s
--   shell: kubectl exec cass-0 -- nodetool status
--     -> cass-0 = 10.244.1.146 (seed/survivor); cass-1 = 10.244.2.125 (victim), both UN

-- STEP 2 — DECOMMISSION the victim so it transitions to LEFT and leaves the ring:
--   shell: kubectl exec cass-1 -- nodetool decommission
--   shell: kubectl exec cass-0 -- nodetool status
--     -> only 10.244.1.146 remains (UN); the victim is gone from the ring.

-- STEP 3 — enable DEBUG at runtime on the SURVIVOR (no restart). At default INFO the spam is
--          invisible in 3.11; the bug surfaces at DEBUG via nodetool setlogginglevel:
--   shell: kubectl exec cass-0 -- nodetool setlogginglevel org.apache.cassandra.net.OutboundTcpConnection DEBUG
--   shell: kubectl exec cass-0 -- nodetool setlogginglevel org.apache.cassandra.gms.Gossiper DEBUG

-- STEP 4 — make the departed node UNREACHABLE (close its pod / port 7000):
--   shell: kubectl scale statefulset/cass --replicas=1
--   shell: kubectl wait --for=delete pod/cass-1 --timeout=60s

-- ============================================================================
-- OBSERVE / BUGGY SIGNATURE (on the SURVIVOR cass-0, /var/log/cassandra/debug.log):
--   The decommissioned node 10.244.2.125 has LEFT the ring (gone from `nodetool status`) yet the
--   survivor still SELECTS it for gossip via the unreachable-member path and repeatedly tries to
--   connect, failing each time on the MessagingService-Outgoing-/<ip>-Gossip thread:
--
--     DEBUG [MessagingService-Outgoing-/10.244.2.125-Gossip] ... OutboundTcpConnection.java:425 - Attempting to connect to /10.244.2.125
--     DEBUG [MessagingService-Outgoing-/10.244.2.125-Gossip] ... OutboundTcpConnection.java:546 - Unable to connect to /10.244.2.125
--     java.net.ConnectException: Connection timed out
--         at sun.nio.ch.Net.connect0(Native Method) ~[na:1.8.0_272]
--         ...
--         at org.apache.cassandra.net.OutboundTcpConnectionPool.newSocket(OutboundTcpConnectionPool.java:146) ~[apache-cassandra-3.11.8.jar:3.11.8]
--         at org.apache.cassandra.net.OutboundTcpConnection.connect(OutboundTcpConnection.java:434) [apache-cassandra-3.11.8.jar:3.11.8]
--         at org.apache.cassandra.net.OutboundTcpConnection.run(OutboundTcpConnection.java:262) [apache-cassandra-3.11.8.jar:3.11.8]
--
--   Supporting evidence the node really LEFT and is still gossiped to:
--     * `nodetool status` on the survivor: 10.244.2.125 NO LONGER in the ring (only 10.244.1.146 UN).
--     * `nodetool gossipinfo` on the survivor: the LEFT node is STILL in the endpoint-state map:
--         /10.244.2.125  STATUS:173:LEFT,-1027438880374084589,1781512981073
--     * Sustained: two distinct "Unable to connect to /10.244.2.125" failures ~2 min apart, ALL
--       AFTER the victim was convicted LEFT/DOWN (conviction 08:43:49; quarantine over 08:44:01;
--       failures 08:46:00, 08:48:11). The LEFT node keeps being selected as long as it stays in
--       the gossip state map.
--
--   A/B CONTROL (fixed cassandra:3.11.9, IDENTICAL 2-node ring + IDENTICAL sequence):
--     After the victim is convicted LEFT/DOWN there are ZERO further "Attempting to connect" and
--     ZERO "Unable to connect" lines for it (observed ~7 min). The fix stops SELECTING the LEFT
--     node for gossip. Both versions keep the LEFT node in `nodetool gossipinfo` (normal expiry);
--     the discriminator is whether the survivor picks it for gossip and tries to connect.
-- ============================================================================
"""

    # Deliberately NOT set (stub): continuous_reproducer stays False and expected_output stays
    # None. The reproducer above is not runnable as single-cluster CQL (it needs a real 2-node ring
    # with a decommissioned, then unreachable, peer), so attaching a looping reproducer pod /
    # mitigation oracle would falsely report "reproduced" on the single node the standard deploy
    # provides. This stub contributes only the diagnosis oracle (root cause) until a multi-node
    # ring harness exists.
    continuous_reproducer = False
    # NOTE: this is a log-spam bug (repeated connection-failure DEBUG lines targeting a departed
    # node), NOT a wrong-result-that-persists-a-value bug — no incorrect value is returned or
    # persisted by any query — so expected_output is intentionally unset.
