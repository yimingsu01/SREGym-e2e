# CASSANDRA-21132 Reproduction Evidence Log

**Bug:** Optionally force IndexStatusManager to use the optimized index status format
**Buggy version:** cassandra:5.0.6 (fix released in 5.0.7)
**Disposition:** REPRODUCED (verbatim AssertionError signature captured)
**Topology:** 2-node ring (StatefulSet) in kind, namespace `repro-21132`
**Date:** 2026-06-11

---

## 1. Bug summary / reproducer extracted from JIRA

Primary source: `/tmp/jira_issues/CASSANDRA-21132.json`

A homogeneous 5.0.x cluster (5.0.4 / 5.0.6) with a **large number of keyspaces/tables/SAI
indexes** enters a **startup deadlock**. On a cold restart (full bring-down / bring-up), gossip
falls back to the pre-5.0.3 (uncompressed, "legacy") SAI index-status encoding because
`Gossiper.getMinVersion()` returns null/unknown before the cluster has converged (no peer
RELEASE_VERSION advertised yet). The legacy encoding duplicates the full keyspace name per index
entry and writes status strings ("BUILD_SUCCEEDED") instead of numeric codes. With enough indexes,
the encoded INDEX_STATUS string exceeds `Short.MAX_VALUE` (32767) bytes, and serialization trips an
`AssertionError` at `TypeSizes.sizeof(TypeSizes.java:44)` (a bare `assert length <= Short.MAX_VALUE`,
hence message `null`). The gossip ACK can never be sent, the joining node stays DOWN, gossip never
converges, the compressed format is never enabled -> deadlock.

### Exact reproducer (from JIRA description):
- 3 nodes (reproduced here with 2; one send-target is enough to trip the size assert), all same version
- Large number of keyspaces/tables/SAI indexes (reporter: ~6 large application keyspaces)
- Full bring-down then bring-up (NOT a single rolling bounce — a single bounce keeps the cached
  peer version so minVersion stays known and the compressed format is kept)
- Observed signature (verbatim from JIRA): RuntimeException -> AssertionError in GossipStage:1 at
  `TypeSizes.sizeof(TypeSizes.java:44)` via VersionedValue/EndpointState/GossipDigestAck serialize.

---

## 2. Deploy

Manifest: `/tmp/repro-21132-ss.yaml` — 2-replica StatefulSet, image `cassandra:5.0.6`,
`podManagementPolicy: OrderedReady`, **volumeClaimTemplates `data` (3Gi, storageClass=standard)
mounted at /var/lib/cassandra** so the schema survives the restart (essential — the restart is the
trigger mechanism).

```
$ kubectl apply -f /tmp/repro-21132-ss.yaml
service/cass created
statefulset.apps/cass created
```

Ring converged (both UN) before loading schema:
```
$ kubectl exec -n repro-21132 cass-0 -- nodetool status
Datacenter: dc1
--  Address      Load        Tokens  Owns (effective)  Host ID                               Rack
UN  10.244.3.11  119.83 KiB  16      100.0%            8f67e8d8-...  rack1
UN  10.244.2.13  109.67 KiB  16      100.0%            8168b821-...  rack1
```

---

## 3. Bloat the SAI index-status gossip payload

DDL file: `/tmp/schema-21132.cql` — 20 keyspaces (RF=2) x 5 tables x 8 SAI indexes, all identifiers
**padded to the 48-char max** (keyspace, table, column, index names). The legacy gossip format
duplicates the 48-char keyspace name per entry, so each entry costs ~120 bytes.

```
$ kubectl exec -i -n repro-21132 cass-0 -- cqlsh --request-timeout=120 < /tmp/schema-21132.cql
```

Index creation is slow (per-DDL schema agreement across the 2-node ring, ~18 indexes/min). Loaded
to **324 indexes** before bring-down (target ~300 to exceed the 32767 byte assert threshold).

State BEFORE bring-down (converged cluster uses the COMPRESSED format — numeric codes, grouped by KS):
```
$ kubectl exec -n repro-21132 cass-0 -- cqlsh -e "SELECT count(*) FROM system_schema.indexes;"
 324
$ kubectl exec -n repro-21132 cass-0 -- nodetool gossipinfo | grep INDEX_STATUS
  INDEX_STATUS:3351:{"system":{"PaxosUncommittedIndex":3},"ks_3xxx...":{"ix3_4_7_159xxx...":3,"ix3_3_1_145xxx...   <-- numeric :3 codes, grouped
  (length = 17654 chars; well under 32767)
```

---

## 4. Trigger: full bring-down then bring-up

```
$ kubectl scale sts/cass -n repro-21132 --replicas=0
statefulset.apps/cass scaled         # all pods gone after ~20s

$ kubectl scale sts/cass -n repro-21132 --replicas=2
statefulset.apps/cass scaled         # cold bring-up; OrderedReady => cass-0 first
```

cass-0 came up and became Ready; the moment cass-1 started and the two exchanged gossip,
cass-0's GossipStage threw.

---

## 5. VERBATIM BUGGY SIGNATURE (cass-0 system log)

```
ERROR [GossipStage:1] 2026-06-11 21:51:36,236 JVMStabilityInspector.java:70 - Exception in thread Thread[GossipStage:1,5,GossipStage]
java.lang.RuntimeException: java.lang.AssertionError
	at org.apache.cassandra.net.InboundSink.accept(InboundSink.java:108)
	at org.apache.cassandra.net.InboundSink.accept(InboundSink.java:45)
	at org.apache.cassandra.net.InboundMessageHandler$ProcessMessage.run(InboundMessageHandler.java:430)
	at org.apache.cassandra.concurrent.ExecutionFailure$1.run(ExecutionFailure.java:133)
	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(Unknown Source)
	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(Unknown Source)
	at io.netty.util.concurrent.FastThreadLocalRunnable.run(FastThreadLocalRunnable.java:30)
	at java.base/java.lang.Thread.run(Unknown Source)
Caused by: java.lang.AssertionError: null
	at org.apache.cassandra.db.TypeSizes.sizeof(TypeSizes.java:44)
	at org.apache.cassandra.gms.VersionedValue$VersionedValueSerializer.serializedSize(VersionedValue.java:381)
	at org.apache.cassandra.gms.VersionedValue$VersionedValueSerializer.serializedSize(VersionedValue.java:359)
	at org.apache.cassandra.gms.EndpointStateSerializer.serializedSize(EndpointState.java:401)
	at org.apache.cassandra.gms.EndpointStateSerializer.serializedSize(EndpointState.java:357)
	at org.apache.cassandra.gms.GossipDigestAckSerializer.serializedSize(GossipDigestAck.java:96)
	at org.apache.cassandra.gms.GossipDigestAckSerializer.serializedSize(GossipDigestAck.java:61)
	at org.apache.cassandra.net.Message$Serializer.payloadSize(Message.java:1088)
	at org.apache.cassandra.net.Message.payloadSize(Message.java:1131)
	at org.apache.cassandra.net.Message$Serializer.serializedSize(Message.java:769)
	at org.apache.cassandra.net.Message.serializedSize(Message.java:1111)
	at org.apache.cassandra.net.OutboundConnections.connectionTypeFor(OutboundConnections.java:215)
	at org.apache.cassandra.net.OutboundConnections.connectionFor(OutboundConnections.java:207)
	at org.apache.cassandra.net.OutboundConnections.enqueue(OutboundConnections.java:96)
	at org.apache.cassandra.net.MessagingService.doSend(MessagingService.java:473)
	at org.apache.cassandra.net.OutboundSink.accept(OutboundSink.java:70)
	at org.apache.cassandra.net.MessagingService.send(MessagingService.java:462)
	at org.apache.cassandra.net.MessagingService.send(MessagingService.java:437)
	at org.apache.cassandra.gms.GossipDigestSynVerbHandler.doVerb(GossipDigestSynVerbHandler.java:110)
	at org.apache.cassandra.net.InboundSink.lambda$new$0(InboundSink.java:78)
	at org.apache.cassandra.net.InboundSink.accept(InboundSink.java:97)
	... 7 common frames omitted
```

This is an EXACT match to the JIRA-reported stack (same exception chain, same
`TypeSizes.sizeof(TypeSizes.java:44)` -> VersionedValue -> EndpointState -> GossipDigestAck ->
GossipDigestSynVerbHandler.doVerb(:110) path; only line numbers in EndpointState differ slightly
because 5.0.6 vs the reporter's build, but they are the same `serializedSize` frames).

It LOOPS every ~5s (gossip interval), confirming the deadlock; JVMStabilityInspector logs but does
not halt:
```
ERROR [GossipStage:1] 2026-06-11 21:51:36,236 ... Exception in thread Thread[GossipStage:1,...]
ERROR [GossipStage:1] 2026-06-11 21:51:41,166 ...
ERROR [GossipStage:1] 2026-06-11 21:51:46,172 ...
ERROR [GossipStage:1] 2026-06-11 21:51:51,173 ...
ERROR [GossipStage:1] 2026-06-11 21:51:56,173 ...
ERROR [GossipStage:1] 2026-06-11 21:52:01,175 ...
...
```

---

## 6. Corroborating evidence

### (a) Deadlock — joining node cannot come up
```
$ kubectl get pods -n repro-21132
NAME     READY   STATUS    RESTARTS   AGE
cass-0   1/1     Running   0          2m9s
cass-1   0/1     Running   0          29s

$ kubectl exec -n repro-21132 cass-0 -- nodetool status
Datacenter: dc1
--  Address      Load        Tokens  Owns  Host ID                               Rack
DN  10.244.2.13  ?           16      ?     8168b821-...  rack1     <-- cass-1 DOWN, cannot join
UN  10.244.3.16  453.55 KiB  16      ?     8f67e8d8-...  rack1
```
(cass-1 itself logs 0 TypeSizes asserts — it never receives the schema because the gossip ACK from
cass-0 is never serialized/sent; it is the *victim* of cass-0's serialization failure.)

### (b) Gossip format REVERTED to legacy (the JIRA "duplicated keyspace names + string status" signal)
After the cold restart, cass-0's INDEX_STATUS is the LEGACY format and is over the 32767 limit:
```
$ kubectl exec -n repro-21132 cass-0 -- nodetool gossipinfo | grep INDEX_STATUS
  INDEX_STATUS:671:{"ks_5xxx...ix5_1_6_214xxx...":"BUILD_SUCCEEDED","ks_7xxx...ix7_1_4_292xxx...":"BUILD_SUCCEEDED","ks_6xxx...":"BUILD_SUCCEEDED",...
  length = 38655 chars   (> 32767 = Short.MAX_VALUE => trips the assert)
```
Note the contrast vs the pre-restart compressed line (Section 3): full `keyspace.index` keys
duplicated per entry + literal `"BUILD_SUCCEEDED"` strings instead of numeric `:3` codes. This is
precisely the pre-5.0.3 format the reporter described, and the 38655-byte length confirms the
overflow mechanism (compressed was only 17654 at the same index count).

---

## 7. A/B control (cassandra:5.0.7 = fix) — NOT RUN; IMPORTANT CAVEAT

Control was NOT run in this session (budget). **The fix is OPT-IN, so a naive stock-5.0.7 A/B with
the identical manifest is expected to STILL REPRODUCE the bug, not go clean.**

Verified from the JIRA issue + CHANGES.txt: CASSANDRA-21132 ("Optionally force IndexStatusManager to
use the optimized index status format") does NOT fix the underlying `getMinVersion()` convergence
race. Instead it adds a new opt-in `cassandra.yaml` option:

  - flag name: `force_optimized_index_status_format`
  - default: `false`  (opt-in)
  - JIRA comment: "the official workaround here, once this is released, will be to set
    force_optimized_index_status_format: true in cassandra.yaml"

Implications for the control:
  * Stock `cassandra:5.0.7` with my IDENTICAL manifest (flag unset => false) will, on a cold
    bring-down/bring-up with 324 indexes, STILL fall back to the legacy format and STILL trip the
    `TypeSizes.sizeof(TypeSizes.java:44)` assert. So a careless A/B would wrongly look like 5.0.7
    "also reproduces", NOT a clean control.
  * The CORRECT positive control on 5.0.7 is: add `force_optimized_index_status_format: true` to
    cassandra.yaml (e.g. via an init/postStart edit), load the SAME `/tmp/schema-21132.cql`, full
    bring-down/bring-up, and confirm both pods reach UN with NO `TypeSizes.sizeof` assertion and
    `gossipinfo` showing the compressed numeric format throughout.
  * cassandra:5.0.7 exists on Docker Hub (<= ceiling), so this controlled A/B is feasible; skipped
    here only to prioritize capturing the buggy signature within budget.

---

## 8. Verdict

**REPRODUCED.** Verbatim signature captured:
`Caused by: java.lang.AssertionError: null  at org.apache.cassandra.db.TypeSizes.sizeof(TypeSizes.java:44)`
in `Thread[GossipStage:1]` via the GossipDigestAck serialization path, on a homogeneous 2-node
cassandra:5.0.6 ring with 324 SAI indexes after a full cold restart, with the gossip format
demonstrably reverted to the legacy (>32767-byte) encoding and the joining node stuck DOWN.
