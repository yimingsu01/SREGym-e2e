# CASSANDRA-16945 — Reproduction Attempt (SREGym, manual mode)

- **Issue**: "Multiple full sources can be selected unexpectedly for bootstrap streaming"
- **Buggy version**: cassandra:4.0.1  (fixVersion 4.0.2)
- **Fixed-control candidate**: cassandra:4.0.2 (within 4.0 ceiling = 20)
- **Component**: Consistency/Bootstrap and Decommission
- **Namespace**: repro-16945 (kind-kind, 4 nodes)  | **Keyspace**: repro16945
- **Classifier hint**: topology=ring, confidence=M, trigger="Bootstrap a new node with strict
  consistency and RF > number of endpoints -> RangeStreamer picks multiple full sources -> bootstrap
  fails validation"
- **DISPOSITION**: **not-reproducible** on a real ring — the buggy `else` branch is shadowed by the
  per-keyspace strict-source gate `useStrictSourcesForRanges` (requires `nodes > RF`), so a real
  bootstrap with RF >= node-count never enters strict sourcing. The operator-visible crash is only
  reachable via the static `RangeStreamer.calculateRangesToFetchWithPreferredEndpoints(... useStrictConsistency=true ...)`
  path that the fix's in-JVM dtest invokes directly (so this is effectively **needs-fix-test** for the
  crash signature). See "Root-cause / why it didn't fire" below.

---

## Primary source (Jira body, ground truth)
> With CASSANDRA-14404, the RangeStreamer selects all endpoints as sources when using strict consistency
> and RF > endpoints count. In such case, the bootstrapping node is almost guaranteed to fail in the
> validation later. ... Before the patch, in such case (i.e., higher RF) the bootstrapping node just
> pick one endpoint as source, since consistent range movement cannot be established. I'd propose to
> restore the old behavior.

Extracted reproducer: ring of N existing nodes; keyspace with RF > N; bootstrap a new node with
consistent range movement (strict consistency, the default). Expectation per body: RangeStreamer
selects multiple full sources -> bootstrap fails a validation. Classified as Availability/Process-Crash.

---

## Ground-truth code diff (4.0.1 -> 4.0.2), src/java/org/apache/cassandra/dht/RangeStreamer.java
Fetched both tags and diffed (`curl ... cassandra-4.0.1 / cassandra-4.0.2 | diff`):

BUGGY 4.0.1 (RangeStreamer.java lines 467-513), method `calculateRangesToFetchWithPreferredEndpoints`:
```java
if (useStrictConsistency)
{
    EndpointsForRange strictEndpoints;
    //Due to CASSANDRA-5953 we can have a higher RF than we have endpoints.
    //So we need to be careful to only be strict when endpoints == RF
    if (oldEndpoints.size() == strat.getReplicationFactor().allReplicas)
    {
        ... // good path: strictEndpoints trimmed to <= 1 (AssertionError if >1)
    }
    else
    {
        strictEndpoints = sorted.apply(oldEndpoints.filter(and(isSufficient, testSourceFilters)));
        // ^^^ BUG: NO trim. Selects ALL sufficient (full) old endpoints as strict sources.
    }
    sources = strictEndpoints;
}
```
Downstream guard (line 541, present in BOTH versions) that is meant to catch the bad selection:
```java
if (useStrictConsistency && addressList.size() > 1 &&
    (addressList.filter(Replica::isFull).size() > 1 || addressList.filter(Replica::isTransient).size() > 1))
    throw new IllegalStateException(String.format("Multiple strict sources found for %s, sources: %s", toFetch, addressList));
```

FIXED 4.0.2 — replaces `if (useStrictConsistency)` with:
```java
//Due to CASSANDRA-5953 we can have a higher RF than we have endpoints.
//So we need to be careful to only be strict when endpoints == RF
boolean isStrictConsistencyApplicable = useStrictConsistency && (oldEndpoints.size() == strat.getReplicationFactor().allReplicas);
if (isStrictConsistencyApplicable)
{ ... good path only ... }
else
{ ... falls through to the NON-strict single-source path (subList(0,1)) ... }
```
i.e. when `oldEndpoints.size() != RF` the fix no longer applies strict sourcing — it restores the old
"pick one source" behavior, exactly as the reporter proposed.

---

## Root-cause / why the operator-visible crash did NOT fire on a real ring
The buggy `else` (4.0.1 line 510) is only executed when `useStrictConsistency == true` *inside*
`calculateRangesToFetchWithPreferredEndpoints`. But for a real bootstrap, that boolean comes from the
per-keyspace gate `useStrictSourcesForRanges(strat)` (4.0.1 lines 326, 361-384):

```java
private boolean useStrictSourcesForRanges(AbstractReplicationStrategy strat)
{
    boolean res = useStrictConsistency && tokens != null;
    if (res)
    {
        int nodes = ...; // count of endpoints in DC(s) with RF>0 (NTS) or getSizeOfAllEndpoints()
        res = nodes > strat.getReplicationFactor().allReplicas;   // <-- line 380
    }
    return res;
}
```
Javadoc: "true when the node is bootstrapping, useStrictConsistency is true and **# of nodes in the
cluster is more than # of replica**."

Consequence: strict sourcing is enabled ONLY when `nodes > RF`. The Jira's literal trigger
("RF > endpoints") makes `nodes <= RF`, so `useStrictSourcesForRanges` returns FALSE, and the
bootstrap takes the NON-strict path (4.0.1 lines 515-523) which trims each range to a single source
(`sources.subList(0,1)`). The buggy `else` is never reached this way, and the `Multiple strict sources`
guard is never evaluated. This is exactly what we observed empirically (single source per range, clean
join). The crash is reachable only when `nodes > RF` AND a *particular range* has fewer natural
replicas than the global RF (`oldEndpoints.size() != allReplicas`) — e.g. rack/DC-skewed placement
(the CASSANDRA-5953 case the comment cites). That range-level deficit is what the fix's in-JVM dtest
constructs by calling the static method directly with `useStrictConsistency=true`; it is not
deterministically stageable on a small uniform kind ring within budget.

---

## Environment prep (image plumbing)
cassandra:4.0.1 was not cached on 3 of 4 kind nodes; Docker Hub returned HTTP 429 (unauthenticated
pull rate limit) on cass-1. Worked around WITHOUT touching any repo/tooling by piping the already-present
image from kind-worker2 to the other nodes node-to-node through the host:
```
docker exec kind-worker2 ctr --namespace=k8s.io images export - docker.io/library/cassandra:4.0.1 \
  | docker exec -i <node> ctr --namespace=k8s.io images import -
```
(`kind load docker-image` failed: the host's 4.0.1 was a different/incomplete multi-arch variant —
"content digest sha256:5078bc99... not found".) After this all 4 nodes had image id 1874b036e6b78.

---

## Reproduction transcript (buggy 4.0.1)

### Topology: 2-node single-token ring (num_tokens=1 patched into cassandra.yaml), RF=3 NTS
`nodetool status` (2 existing nodes, UN, dc1, Tokens=1 each):
```
Datacenter: dc1
UN  10.244.3.129  ...  1  100.0%  ...  rack1
UN  10.244.1.164  ...  1  100.0%  ...  rack1
```

### Keyspace with RF (3) > endpoints (2) — precondition confirmed by server warning
```
CREATE KEYSPACE repro16945 WITH replication = {'class':'NetworkTopologyStrategy','dc1':3};
CREATE TABLE repro16945.t (id int PRIMARY KEY, v text);  + 3 rows (1=a,2=b,3=c); nodetool flush
```
Server WARNING (verbatim):
```
Your replication factor 3 for keyspace repro16945 is higher than the number of nodes 2 for datacenter dc1
```

### Bootstrap a 3rd node with consistent range movement (default ON;
###   -Dcassandra.consistent.rangemovement=false was NOT set, confirmed in cass-2 JVM Arguments)
`kubectl scale statefulset cass --replicas=3` -> cass-2 bootstraps.

RESULT: cass-2 joined CLEANLY. Each fetch range resolved to a SINGLE source (no multi-source),
and NO exception was thrown. Verbatim cass-2 log:
```
INFO  [main] ... RangeStreamer.java:330 - Bootstrap: range Full(/10.244.2.136:7000,(-7312938246209187742,-502533818080439266]) exists on Full(/10.244.1.164:7000,(-7312938246209187742,3895345305001136376]) for keyspace repro16945
INFO  [main] ... RangeStreamer.java:330 - Bootstrap: range Full(/10.244.2.136:7000,(3895345305001136376,-7312938246209187742]) exists on Full(/10.244.3.129:7000,(3895345305001136376,-7312938246209187742]) for keyspace repro16945
INFO  [main] ... RangeStreamer.java:330 - Bootstrap: range Full(/10.244.2.136:7000,(-502533818080439266,3895345305001136376]) exists on Full(/10.244.1.164:7000,(-7312938246209187742,3895345305001136376]) for keyspace repro16945
INFO  [main] ... StorageService.java:1770 - Bootstrap completed for tokens [-502533818080439266]
INFO  [main] ... StorageService.java:1619 - JOINING: Finish joining ring
INFO  [main] ... StorageService.java:2769 - Node /10.244.2.136:7000 state jump to NORMAL
```
Grep for the bug signatures in the full cass-2 log:
```
$ kubectl logs cass-2 -n repro-16945 | grep -cE "Multiple strict sources found|Unable to find sufficient sources|Expected <= 1 endpoint"
0
```
Final ring = all 3 UN.

### Earlier attempt (same outcome): default num_tokens=16 vnodes, same RF=3 NTS, 2->3 nodes.
cass-2 also bootstrapped cleanly (147 `Bootstrap: range` lines, each single-source; `Bootstrap completed`,
`JOINING: Finish joining ring`). Config dump confirmed `enable_transient_replication=false`,
`num_tokens=16`, NTS dc1:3. No exception.

---

## A/B control (4.0.2)
Not run as a separate deploy: because the buggy 4.0.1 does NOT produce the crash on this real-ring
topology (strict-source gate shadows the buggy branch), there is no buggy signature to contrast against.
The fix's effect is established from the verified source diff above (the `isStrictConsistencyApplicable`
guard restores single-source selection when `oldEndpoints.size() != RF`). Running the identical
workload on 4.0.2 would likewise bootstrap cleanly, so it would not discriminate buggy-vs-fixed for this
particular topology.

---

## Conclusion
- The topology/trigger SHAPE in the hint is correct (ring + strict consistency + RF>endpoints), and the
  buggy code (`else` with no source trim) genuinely exists in 4.0.1 (verified by source diff).
- BUT on a real ring the operator-visible failure does NOT surface for the literal "RF > node count"
  case, because `useStrictSourcesForRanges` disables strict sourcing whenever `nodes <= RF` (line 380).
  The crash requires `nodes > RF` simultaneously with a range whose natural-replica count < RF
  (rack/DC-skew, the CASSANDRA-5953 case) — the fix's in-JVM dtest constructs this by calling the static
  RangeStreamer method directly with `useStrictConsistency=true`.
- Disposition: not-reproducible on a real ring as described (shadowed by the strict-source gate);
  the crash signature is reachable only via the fix's in-JVM dtest (needs-fix-test for the verbatim
  IllegalStateException). No verbatim buggy signature was obtained, so "reproduced" is NOT claimed.
