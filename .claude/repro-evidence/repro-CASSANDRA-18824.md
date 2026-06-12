# CASSANDRA-18824 reproduction attempt — DISPOSITION: not-reproducible (on assigned buggy image 4.1.3)

## Candidate
- Issue: CASSANDRA-18824 — "Backport CASSANDRA-16418: Cleanup behaviour during node decommission caused missing replica"
- Assigned buggy version: **4.1.3**
- Classifier HINT: topology=ring, confidence=H, trigger="2-node RF=1, decommission node1 while looping nodetool cleanup on node2 -> cleanup ignores pending ranges -> rows lost"
- Components: Consistency/Bootstrap and Decommission
- fixVersions (of 18824): 3.0.30, 3.11.17, 4.0.13, **4.1.4**, 5.0-rc1, 5.0, 6.0-alpha1, 6.0

## Reproducer extracted from the Jira body (ground truth)
STR (verbatim from description):
- Create two node cluster
- Create keyspace with RF=1
- Insert sample data (assert data is available when querying both nodes)
- Start decommission process of node 1
- Start running cleanup in a loop on node 2 until decommission on node 1 finishes
- Verify all rows are in the cluster — it will fail as the previous step removed some rows

Mechanism (from body): cleanup uses only LOCAL ranges, not PENDING ranges, so data just streamed in
during the decommission (held as pending ranges) is treated as redundant and deleted -> silent data loss.

The body explicitly proposes two fixes and notes which was taken:
> "Alternatively we could interrupt/prevent the cleanup process from running when any pending range on a
>  node is detected. That sounds like a reasonable alternative ... relatively easy to implement."
> "The bug has been already fixed in 4.x with CASSANDRA-16418, the goal of this ticket is to backport it to 3.x."

=> 18824 is a **3.x backport** ticket. The 4.x lines were ALREADY fixed by the parent ticket CASSANDRA-16418.

## Why 4.1.3 is the WRONG buggy image (source-based determination; no deploy needed)

### 1. CHANGES.txt (authoritative version attribution), tag cassandra-4.1.4
- CASSANDRA-16418 entry appears under the **4.1.1** release section, text:
  "Add safeguard so cleanup fails when node has pending ranges"
- CASSANDRA-18824 does **NOT** appear anywhere in the 4.1.x CHANGES.txt
  => the 4.1.4 fixVersion on 18824 is a forward-merge no-op (Cassandra merges 3.x -> 4.0 -> 4.1 -> 5.0 -> trunk
     and stamps fixVersions on every newer branch even when the code there is unchanged).

### 2. Primary-source code: the CASSANDRA-16418 guard is PRESENT in 4.1.3
File: src/java/org/apache/cassandra/service/StorageService.java @ tag cassandra-4.1.3, method forceKeyspaceCleanup
(this is the nodetool cleanup entry point; it runs BEFORE CompactionManager.performCleanup):

```java
public int forceKeyspaceCleanup(int jobs, String keyspaceName, String... tables) throws IOException, ExecutionException, InterruptedException
{
    if (SchemaConstants.isLocalSystemKeyspace(keyspaceName))
        throw new RuntimeException("Cleanup of the system keyspace is neither necessary nor wise");

    if (tokenMetadata.getPendingRanges(keyspaceName, getBroadcastAddressAndPort()).size() > 0)
        throw new RuntimeException("Node is involved in cluster membership changes. Not safe to run cleanup.");
    ...
}
```
This is exactly "solution 2" from the body: cleanup is REJECTED (not silent data loss) whenever the node has
pending ranges — i.e., precisely the decommission/streaming window the reproducer targets.

### 3. The guard is ABSENT in the genuinely-buggy 4.1.0
File: same path @ tag cassandra-4.1.0, method forceKeyspaceCleanup:
```java
public int forceKeyspaceCleanup(int jobs, String keyspaceName, String... tables) throws IOException, ExecutionException, InterruptedException
{
    if (SchemaConstants.isLocalSystemKeyspace(keyspaceName))
        throw new RuntimeException("Cleanup of the system keyspace is neither necessary nor wise");
    // NO pending-ranges guard here
    CompactionManager.AllSSTableOpStatus status = CompactionManager.AllSSTableOpStatus.SUCCESSFUL;
    ...
}
```
grep "Not safe to run cleanup": 4.1.0 -> 0 matches; 4.1.3 -> 1 match; 4.1.4 -> 1 match.

### 4. performCleanup (CompactionManager.java @ 4.1.3) still uses only local replicas
The data-loss code path is unchanged (it never considered pending ranges); 16418 fixed the bug at the
nodetool entry point by refusing cleanup, not by changing performCleanup:
```java
// CompactionManager.performCleanup @ 4.1.3
final RangesAtEndpoint replicas = StorageService.instance.getLocalReplicas(keyspace.getName());
// ... no pending-range awareness; this is reached only if the StorageService guard passes
```

## Conclusion
On the assigned buggy image **cassandra:4.1.3**, the CASSANDRA-16418 safeguard is already compiled in.
Running `nodetool cleanup` on node2 while node1 is decommissioning (node2 has pending ranges) would be
REJECTED with `RuntimeException: Node is involved in cluster membership changes. Not safe to run cleanup.`
rather than silently deleting rows. The STR's "cleanup removes some rows" outcome cannot occur on 4.1.3.

A live reproduction of the data-loss symptom requires Cassandra **<= 4.1.0** (or <= 4.0.7 on the 4.0 line),
which is outside this candidate's assigned version. Therefore: **not-reproducible on the given buggy image.**

## Control reasoning
No dynamic A/B was run (source determination is dispositive). For completeness:
- cassandra:4.1.4 (the suggested fixed control) carries the IDENTICAL guard as 4.1.3 — both would reject
  the cleanup. So a 4.1.3-vs-4.1.4 A/B would show NO divergence, further confirming 4.1.3 is not buggy.
- The true buggy/fixed pair would be 4.1.0 (no guard, data loss) vs 4.1.1 (guard added by 16418).

## Tag correction
- topology=ring is correct for the underlying bug, but the assigned buggy VERSION (4.1.3) is wrong:
  classifier computed 4.1.4 - 1 = 4.1.3 from 18824's fixVersions, not realizing the 4.1 line was fixed by
  the PARENT ticket CASSANDRA-16418 at 4.1.1, and that 18824's 4.1.4 fixVersion is a forward-merge no-op
  (18824 is a 3.x backport; it does not appear in 4.1.x CHANGES.txt).

## Namespaces created: NONE (source-based determination; no pods deployed). Nothing to tear down.
