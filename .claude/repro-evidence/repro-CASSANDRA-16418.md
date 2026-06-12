# CASSANDRA-16418 — Reproduction Evidence

**Summary:** Unsafe to run `nodetool cleanup` during bootstrap or decommission.
**Buggy version:** cassandra:4.1.0  |  **Fixed control:** cassandra:4.1.1 (fix commit `8bb9c72f582de6bcc39522ba9ade91fd5bc22f67`)
**Components:** Consistency/Bootstrap and Decommission  |  **Fix versions:** 4.0.8, 4.1.1, 5.0-alpha1, 5.0
**Namespace:** repro-16418 (kind-kind)  |  **Keyspace:** repro16418 (RF=1)
**Disposition:** REPRODUCED

## Reproducer extracted from Jira body (ground truth)
> "We ran a cleanup during a decommission. All the streamed data was silently deleted, the bootstrap did
> not fail, the cluster's data after the decommission was very different to the state before. Cleanups do
> not take into account pending ranges and so the cleanup thought that all the data that had just been
> streamed was redundant and so deleted it."

The fix adds a guard in `StorageService.forceKeyspaceCleanup()`:
```java
if (tokenMetadata.getPendingRanges(keyspaceName, getBroadcastAddressAndPort()).size() > 0)
    throw new RuntimeException("Node is involved in cluster membership changes. Not safe to run cleanup.");
```
On 4.1.0 this guard is ABSENT, so cleanup runs on a surviving node that is *receiving* a decommissioning
peer's ranges (pending ranges), and deletes the just-streamed data.

## Tag correction
Classifier hint said topology=ring, trigger "cleanup concurrent with bootstrap/decommission -> streamed
data silently deleted". The Jira body's VERIFIED scenario is **decommission** (bootstrap symmetry was
explicitly *unverified* by the reporter, and 4.1.0 still has an `isJoined()` guard in
`CompactionManager.performCleanup()` that no-ops cleanup on a JOINING node). So the reproducer runs cleanup
on the **surviving receiver** (which is `isJoined()==true`, sailing past the old guard — exactly the hole
the fix closes). Topology=ring is correct.

## Topology
2-node ring, RF=1 (SimpleStrategy). RF=1 chosen so deleted data has no redundant replica to mask the loss.
cass-0 owns 51.2%, cass-1 owns 48.8%. Decommission cass-1 → its ranges stream to cass-0 and become pending
on cass-0. Stream throttled to 1 Mb/s (`nodetool setstreamthroughput 1`) to widen the destructive window.
Data fattened to ~9 MB/node with INCOMPRESSIBLE random payloads (hex of os.urandom) so the throttled stream
lasts ~70 s (repeated-char payloads compress to ~0 under LZ4 and gave only ~2 s — insufficient).

---

## BUGGY 4.1.0 — verbatim transcript

### Baseline (after loading 7000 rows, flush both nodes)
```
$ kubectl exec -n repro-16418 cass-0 -- cqlsh -e "CONSISTENCY ONE; SELECT COUNT(*) FROM repro16418.t;"
 count
-------
  7000
```
On-disk: cass-0 8.47 MiB, cass-1 8.79 MiB (incompressible).

### Forceful decommission of cass-1 (background) + cleanup loop on cass-0
(`--force` required because system_distributed has RF=3 with N=2:
`nodetool: Unsupported operation: Not enough live nodes to maintain replication factor in keyspace system_distributed (RF = 3, N = 2). Perform a forceful decommission to ignore.`)

```
===== iter 1  04:16:47 =====   [netstats] Mode: NORMAL   [cleanup] EXIT=0   [count] 7000
===== iter 2  04:16:53 =====   [netstats] Mode: NORMAL   [cleanup] EXIT=0   [count] 7000
===== iter 3  04:16:59 =====   [netstats] Mode: NORMAL   [cleanup] EXIT=0   [count] 7000
===== iter 4  04:17:04 =====   [netstats] Mode: NORMAL   [cleanup] EXIT=0   [count] 7000
===== iter 5  04:17:10 =====
[netstats]   Receiving 28 files, 9122082 bytes total. Already received 2 files (7.14%), 28055 bytes total (0.31%)
[cleanup]    EXIT=0           <-- cleanup SUCCEEDS with NO guard while node is receiving streamed data
[count]      3375             <-- SILENT DATA LOSS: 7000 -> 3375
>>>>>> DATA LOSS: count=3375 (was 7000) at iter 5 <<<<<<
```

### Durable loss (scale replicas=1 so cass-1 cannot rejoin/re-stream)
```
$ kubectl scale statefulset/cass -n repro-16418 --replicas=1
$ kubectl exec -n repro-16418 cass-0 -- nodetool status repro16418
UN  10.244.2.128  8.49 MiB  16      100.0%   ad5b19dc-...  rack1     (single node, owns 100%)

$ kubectl exec -n repro-16418 cass-0 -- cqlsh -e "CONSISTENCY ONE; SELECT COUNT(*) FROM repro16418.t;"
 count
-------
  3375
```
~3625 rows permanently lost. Matches "the cluster's data after the decommission was very different to the
state before." This is the VERBATIM BUGGY SIGNATURE (wrong query result: 3375 rows instead of 7000).

---

## FIXED 4.1.1 — A/B control (identical workload + topology)

Same 2-node RF=1 ring on cassandra:4.1.1, throttled, keyspace repro16418. Forceful decommission of cass-1
in background; cleanup loop on cass-0. At iter 1 cass-1 is in `UL` (Leaving) state (pending ranges live on
cass-0):

```
[status] UL  10.244.2.131 ... 51.2% ... | UN  10.244.3.125 ... 48.8% ...
[cleanup]
error: Node is involved in cluster membership changes. Not safe to run cleanup.
-- StackTrace --
java.lang.RuntimeException: Node is involved in cluster membership changes. Not safe to run cleanup.
	at org.apache.cassandra.service.StorageService.forceKeyspaceCleanup(StorageService.java:3810)
	... (JMX/RMI frames) ...
command terminated with exit code 2
>>>>>> FIXED GUARD FIRED at iter 1 <<<<<<
```

Fixed image REJECTS cleanup (exit 2, RuntimeException at `StorageService.forceKeyspaceCleanup:3810`) the
instant pending ranges exist — no data is deleted. Exactly the guard added by the fix commit.

---

## Conclusion
- BUGGY 4.1.0: `nodetool cleanup` on the surviving receiver during a decommission succeeds silently (EXIT=0)
  and DELETES streamed data → COUNT 7000 → 3375 (durable).
- FIXED 4.1.1: same operation throws `Node is involved in cluster membership changes. Not safe to run cleanup.`
  and deletes nothing.
- Clean A/B contrast. Bug REPRODUCED with verbatim signature (wrong COUNT) + fixed-image control.

## Notes / SREGym tooling findings
- StatefulSet auto-recreates a decommissioned pod (it wants N replicas), which can re-bootstrap and re-stream
  data back, masking the loss. Mitigated by `kubectl scale --replicas=1` immediately after the destructive
  cleanup. Not a tooling bug, but relevant for any automated oracle: a decommission-based reproducer on a
  StatefulSet must pin replicas down to observe durable loss.
- A normal decommission at N=2 is blocked by system_distributed RF=3; `nodetool decommission --force` is
  required. This is expected Cassandra behavior, not a bug.
