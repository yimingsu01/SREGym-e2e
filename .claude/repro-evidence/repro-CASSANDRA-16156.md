# CASSANDRA-16156 — Decommissioned nodes are picked for gossip when unreachable nodes are considered

- **Buggy version:** cassandra:3.11.8 (Cluster/Gossip)
- **fixVersions:** 3.0.23, 3.11.9, 4.0
- **Namespace:** repro-16156 (kind-kind, ephemeral storage, no PVC)
- **Disposition:** reproduced
- **Date:** 2026-06-12

## Bug (from Jira body — ground truth)
After a node is decommissioned, it is STILL considered for gossip via the "unreachable"
endpoints path (`Gossiper.maybeGossipToUnreachableMember` -> `sendGossip`), even though it
has LEFT the ring. This produces repeated connection-failure log spam targeting the
departed node. The Jira reporter's stack/log is from a 4.0 in-JVM dtest (new netty messaging
stack: `URGENT_MESSAGES`, NoSpamLogger, netty `AnnotatedConnectException`, loopback `127.0.0.x:7012`).
On the 3.11.8 buggy image the SAME bug fires through the OLD `OutboundTcpConnection` stack
on the `MessagingService-Outgoing-/<ip>-Gossip` thread, port 7000, real pod IPs, DEBUG level.

## Topology / reproducer extracted
Ring required. 2-node ring is sufficient and fastest:
1. Bring up 2-node ring (cass-0 seed = survivor; cass-1 = victim).
2. `nodetool decommission` on cass-1 -> it transitions to LEFT and leaves the ring.
3. Make it unreachable: scale StatefulSet to 1 replica so cass-1's pod (and its port 7000)
   disappears.
4. On the survivor cass-0, enable DEBUG on `org.apache.cassandra.net.OutboundTcpConnection`
   and `org.apache.cassandra.gms.Gossiper` (runtime, no restart).
5. Observe: the survivor keeps selecting the LEFT node's IP for gossip and keeps trying to
   connect to it, failing each time.

## Commands

```
kubectl create ns repro-16156
kubectl apply -f /tmp/repro-16156-ring.yaml          # 2-node StatefulSet, cassandra:3.11.8
kubectl rollout status statefulset/cass -n repro-16156 --timeout=900s
kubectl exec -n repro-16156 cass-0 -- nodetool status
# cass-0 = 10.244.1.146 (seed/survivor); cass-1 = 10.244.2.125 (victim)

kubectl exec -n repro-16156 cass-1 -- nodetool decommission
kubectl exec -n repro-16156 cass-0 -- nodetool status   # only 10.244.1.146 remains (UN)

kubectl exec -n repro-16156 cass-0 -- nodetool setlogginglevel org.apache.cassandra.net.OutboundTcpConnection DEBUG
kubectl exec -n repro-16156 cass-0 -- nodetool setlogginglevel org.apache.cassandra.gms.Gossiper DEBUG
kubectl scale statefulset/cass -n repro-16156 --replicas=1   # closes cass-1 / its port 7000
kubectl wait -n repro-16156 --for=delete pod/cass-1 --timeout=60s
# then inspect /var/log/cassandra/debug.log on cass-0
```

## VERBATIM BUGGY SIGNATURE (cass-0 /var/log/cassandra/debug.log)

The decommissioned node 10.244.2.125 has LEFT the ring (gone from `nodetool status`) yet the
survivor still tries to gossip-connect to it and the connection fails:

```
DEBUG [MessagingService-Outgoing-/10.244.2.125-Gossip] 2026-06-12 08:46:00,222 OutboundTcpConnection.java:546 - Unable to connect to /10.244.2.125
java.net.ConnectException: Connection timed out
	at sun.nio.ch.Net.connect0(Native Method) ~[na:1.8.0_272]
	at sun.nio.ch.Net.connect(Net.java:482) ~[na:1.8.0_272]
	at sun.nio.ch.Net.connect(Net.java:474) ~[na:1.8.0_272]
	at sun.nio.ch.SocketChannelImpl.connect(SocketChannelImpl.java:647) ~[na:1.8.0_272]
	at org.apache.cassandra.net.OutboundTcpConnectionPool.newSocket(OutboundTcpConnectionPool.java:146) ~[apache-cassandra-3.11.8.jar:3.11.8]
	at org.apache.cassandra.net.OutboundTcpConnectionPool.newSocket(OutboundTcpConnectionPool.java:132) ~[apache-cassandra-3.11.8.jar:3.11.8]
	at org.apache.cassandra.net.OutboundTcpConnection.connect(OutboundTcpConnection.java:434) [apache-cassandra-3.11.8.jar:3.11.8]
	at org.apache.cassandra.net.OutboundTcpConnection.run(OutboundTcpConnection.java:262) [apache-cassandra-3.11.8.jar:3.11.8]
```

### Supporting evidence the node really LEFT and is still gossiped to

`nodetool status` on the survivor (10.244.2.125 NO LONGER in the ring):
```
--  Address       Load       Tokens       Owns (effective)  Host ID                               Rack
UN  10.244.1.146  80.05 KiB  256          100.0%            c1426133-f856-4a5d-8b4c-431dfeac84e9  rack1
```

`nodetool gossipinfo` on the survivor (LEFT node STILL in the gossip endpoint-state map):
```
/10.244.2.125
  generation:1781253641
  heartbeat:171
  STATUS:173:LEFT,-1027438880374084589,1781512981073
```

Repeated `-Gossip`-thread connect attempts to the LEFT node AFTER it left
(decommission/Removing-tokens at 08:43:01, marked DOWN/Convicted LEFT at 08:43:49,
gossip quarantine over at 08:44:01) — i.e. the unreachable-member gossip keeps picking it:
```
DEBUG [GossipStage:1] 2026-06-12 08:43:49,343 Gossiper.java:414 - Convicting /10.244.2.125 with status LEFT - alive true
INFO  [GossipStage:1] 2026-06-12 08:43:49,343 Gossiper.java:1119 - InetAddress /10.244.2.125 is now DOWN
DEBUG [MessagingService-Outgoing-/10.244.2.125-Gossip] 2026-06-12 08:43:50,345 OutboundTcpConnection.java:425 - Attempting to connect to /10.244.2.125
DEBUG [GossipTasks:1] 2026-06-12 08:44:01,350 Gossiper.java:921 - 60000 elapsed, /10.244.2.125 gossip quarantine over
DEBUG [MessagingService-Outgoing-/10.244.2.125-Gossip] 2026-06-12 08:46:00,400 OutboundTcpConnection.java:425 - Attempting to connect to /10.244.2.125
DEBUG [MessagingService-Outgoing-/10.244.2.125-Gossip] 2026-06-12 08:48:11,449 OutboundTcpConnection.java:425 - Attempting to connect to /10.244.2.125
```

**Sustained / repeated** — two distinct `Unable to connect` failures ~2 min apart (cadence set
by the OS connect-timeout in kind's blackhole net; each timeout immediately re-triggers the next
attempt). The LEFT node keeps being selected as long as it stays in the gossip state map:
```
DEBUG [MessagingService-Outgoing-/10.244.2.125-Gossip] 2026-06-12 08:46:00,222 OutboundTcpConnection.java:546 - Unable to connect to /10.244.2.125
DEBUG [MessagingService-Outgoing-/10.244.2.125-Gossip] 2026-06-12 08:48:11,292 OutboundTcpConnection.java:546 - Unable to connect to /10.244.2.125
```

## tag_correction
Classifier hint trigger said "Connection refused log spam" via `maybeGossipToUnreachableMember`.
Mechanism and trigger CONFIRMED (decommission -> peer still gossips to the LEFT node). Two
literal-format corrections, neither changes the bug:
1. The Jira's verbatim log is from a 4.0 in-JVM dtest (new netty stack: NoSpamLogger,
   `AnnotatedConnectException`, `URGENT_MESSAGES`, loopback `:7012`). On the actual buggy
   image 3.11.8 the bug surfaces via the OLD `OutboundTcpConnection` stack: thread
   `MessagingService-Outgoing-/<ip>-Gossip`, message `Unable to connect to /<ip>`, port 7000.
2. In the kind overlay network the departed pod IP is blackholed, so the exception is
   `java.net.ConnectException: Connection timed out` rather than "Connection refused".
   Same code path (`OutboundTcpConnection.connect` -> `OutboundTcpConnectionPool.newSocket`),
   same bug — only the OS-level socket error differs (blackhole timeout vs RST refusal).
3. The spam is at DEBUG in 3.11 (default INFO shows nothing); surfaced at runtime with
   `nodetool setlogginglevel` (no restart). Topology hint "ring" CONFIRMED.

## Control (A/B) — cassandra:3.11.9 (fix released; 9 <= 3.11 ceiling 19)

Buggy ring deleted, then an IDENTICAL 2-node ring on cassandra:3.11.9 deployed in the same
namespace; ran the SAME sequence (decommission cass-1 -> enable DEBUG -> scale to 1 -> wait).
Control victim IP = 10.244.2.127.

Result: the fix STOPS gossiping to the LEFT node. Connect attempts to the victim occur ONLY
while it is still alive/leaving; after it is convicted LEFT/DOWN there are ZERO further attempts
and ZERO connection failures, observed for ~7 minutes post-conviction.

```
# victim convicted LEFT / marked DOWN:
DEBUG [GossipStage:1] 2026-06-12 08:56:21,847 Gossiper.java:415 - Convicting /10.244.2.127 with status LEFT - alive true
INFO  [GossipStage:1] 2026-06-12 08:56:21,848 Gossiper.java:1120 - InetAddress /10.244.2.127 is now DOWN
DEBUG [GossipTasks:1] 2026-06-12 08:56:33,857 Gossiper.java:922 - 60000 elapsed, /10.244.2.127 gossip quarantine over

# ALL "Attempting to connect to /10.244.2.127" — every one is BEFORE the 08:56:21 conviction
# (live/leaving gossip, all succeed with "Done connecting"); NONE after:
DEBUG [MessagingService-Outgoing-/10.244.2.127-Gossip] 2026-06-12 08:53:08,688 OutboundTcpConnection.java:425 - Attempting to connect to /10.244.2.127
DEBUG [MessagingService-Outgoing-/10.244.2.127-Small]  2026-06-12 08:53:11,779 OutboundTcpConnection.java:425 - Attempting to connect to /10.244.2.127
DEBUG [MessagingService-Outgoing-/10.244.2.127-Gossip] 2026-06-12 08:55:34,073 OutboundTcpConnection.java:425 - Attempting to connect to /10.244.2.127

# "Unable to connect to /10.244.2.127": (none)
# Last victim -Gossip line is just the socket close at conviction time; no retries afterward:
DEBUG [MessagingService-Outgoing-/10.244.2.127-Gossip] 2026-06-12 08:56:21,849 OutboundTcpConnection.java:411 - Socket to /10.244.2.127 closed
# pod time at final check: 2026-06-12 09:03:34  (~7 min after conviction, still no spam)
```

### A/B contrast (the discriminator = the LEFT state)
| | 3.11.8 (buggy) | 3.11.9 (fixed) |
|---|---|---|
| victim convicted LEFT | 08:43:49 | 08:56:21 |
| connect attempts AFTER conviction | YES — 08:43:50, 08:46:00, 08:48:11 (sustained) | NONE (observed ~7 min) |
| "Unable to connect" failures to LEFT node | YES (08:46:00, 08:48:11) | 0 |

Both versions keep the LEFT node in `nodetool gossipinfo` (normal expiry handling); the bug
and its fix are specifically about whether the survivor SELECTS that LEFT node for gossip and
tries to connect to it. 3.11.8 does (spam); 3.11.9 does not.

## Conclusion
REPRODUCED on cassandra:3.11.8. A decommissioned (LEFT) node is still selected for gossip via
the unreachable-member path, so the survivor repeatedly tries to connect to the departed node
and logs connection failures (`Unable to connect ... ConnectException`) on the
`MessagingService-Outgoing-/<ip>-Gossip` thread. The released fix (cassandra:3.11.9) eliminates
the post-LEFT connection attempts under the identical workload.

