# CASSANDRA-15970 — Reproduction Evidence Log

**Summary (from Jira):** "3.x fails to start if commit log has range tombstones from a column which is also deleted"
**Buggy version under test:** cassandra:3.11.7
**Fix versions (from Jira):** 3.0.22, 3.11.8
**Components:** Legacy/Local Write-Read Paths, Local/Commit Log
**Namespace created:** repro-15970
**Disposition:** confirmed-blocked (missing infrastructure: cannot author 2.1 legacy-format trigger data; required images unpullable due to Docker Hub rate limit)

---

## 1. Exact reproducer extracted from the Jira BODY (ground truth)

The classifier hint said `topology=1node`, trigger = "collection range tombstone written in 2.1 +
collection dropped + upgrade -> empty row with null clustering -> startup crash on commitlog replay".
The Jira body confirms this is an **UPGRADE-PATH** bug. Verbatim mechanism from the body:

* The schema had a collection type in **2.1**
* a collection **range tombstone** happened in **2.1**
* the row only had the RT, no other cells
* the collection was **dropped** in 2.1
* 3.0 detected the collection was deleted and ignored the cell
* 3.0 produced an **empty row with a null clustering key** (since we skipped the RT)
=> on upgrade to 3.0/3.11, startup commitlog replay (or a read) dereferences the null clustering and NPEs.

Buggy signatures quoted in the Jira body (these are what a real reproduction must show):

Startup / commitlog-replay crash:
```
ERROR [node1_isolatedExecutor:1] node1 2020-07-21 18:59:39,048 JVMStabilityInspector.java:102 - Exiting due to error while processing commit log during initialization.
org.apache.cassandra.db.commitlog.CommitLogReplayer$CommitLogReplayException: Unexpected error deserializing mutation; saved to /var/folders/.../mutation....dat.  This may be caused by replaying a mutation against a table with the same name but incompatible schema.
        at org.apache.cassandra.db.commitlog.CommitLogReplayer.handleReplayError(CommitLogReplayer.java:731)
        ...
Caused by: java.lang.NullPointerException: null
        at org.apache.cassandra.db.ClusteringComparator.validate(ClusteringComparator.java:206)
        at org.apache.cassandra.db.partitions.PartitionUpdate.validate(PartitionUpdate.java:494)
        at org.apache.cassandra.db.commitlog.CommitLogReplayer.replayMutation(CommitLogReplayer.java:629)
```

Read-path crash (if drained in 2.2 before upgrade):
```
Caused by: java.lang.NullPointerException: null
        at org.apache.cassandra.db.ClusteringComparator.compare(ClusteringComparator.java:131)
        at org.apache.cassandra.db.UnfilteredDeserializer$OldFormatDeserializer.compareNextTo(UnfilteredDeserializer.java:391)
        at org.apache.cassandra.db.columniterator.SSTableIterator$ForwardReader.handlePreSliceData(SSTableIterator.java:105)
        ...
```

Both crash sites are in the **legacy / old-format deserialization path**
(`UnfilteredDeserializer$OldFormatDeserializer`, `PartitionUpdate.validate` during replay).

---

## 2. Why a faithful reproduction requires a Cassandra 2.1 pod

The malformed on-disk/commitlog structure ("empty row with a null clustering key", produced by skipping
a **legacy collection range tombstone** for a **dropped** collection) only exists in the **2.1 storage
format**. Cassandra 3.11.x writes the 3.0+ `big`/`ma` storage format, in which collection deletions are
represented differently; 3.11.x can *read* 2.1 sstables/commitlog via `OldFormatDeserializer` but
**cannot write** the old-style collection RT that triggers the NPE. The official repro for this ticket is
an in-JVM dtest (`dtest-3.0.21.jar` in the stack frames) that drives a 2.1 -> 3.x upgrade.

Therefore the trigger data MUST be authored by a **cassandra:2.1** pod and then read/replayed by the
**cassandra:3.11.7** (buggy) pod on the same data directory.

---

## 3. Environment / feasibility-gate commands and RAW outputs

Cluster: existing kind cluster, context kind-kind, 4 nodes (1 control-plane + 3 workers). Confirmed Ready.

### Images already present on kind nodes (pre-pulled by earlier sessions)
```
$ docker exec kind-worker crictl images | grep -i cassandra
docker.io/library/cassandra   3.11.19   597351c0039f4   130MB
docker.io/library/cassandra   3.11.6    6e1f443aca8c9   131MB
docker.io/library/cassandra   4.0.0     ...
docker.io/library/cassandra   4.0.20    ...
docker.io/library/cassandra   4.1.0     ...
docker.io/library/cassandra   5.0.3 / 5.0.6 / 5.0.8 ...
```
=> NO cassandra:2.1.x present, and NO cassandra:3.11.7 present. 3.11.6 (buggy, <3.11.8) is present but
useless without 2.1 data; 3.11.19 (fixed) is present.

### Docker Hub tags DO exist (HTTP 200) ...
```
$ for t in 2.1 2.1.22 3.11.7 3.11.8; do curl -s -o /dev/null -w "%{http_code}\n" \
    "https://hub.docker.com/v2/repositories/library/cassandra/tags/$t"; done
cassandra:2.1     -> 200
cassandra:2.1.22  -> 200
cassandra:3.11.7  -> 200   (buggy version under test)
cassandra:3.11.8  -> 200   (fixed-control candidate)
```

### ... but they are UNPULLABLE: anonymous rate limit fully exhausted
```
$ docker pull cassandra:2.1.22
Error response from daemon: error from registry: You have reached your unauthenticated pull rate limit. https://www.docker.com/increase-rate-limit

$ docker exec kind-worker crictl pull docker.io/library/cassandra:2.1.22
... 429 Too Many Requests - Server message: toomanyrequests: You have reached your
unauthenticated pull rate limit ... docker-ratelimit-source: 128.105.145.47

# Registry rate-limit headers (authoritative): remaining is ZERO, window 21600s (6h)
$ curl -sI -H "Authorization: Bearer <anon-token>" \
    https://registry-1.docker.io/v2/library/cassandra/manifests/2.1.22 | grep -i ratelimit
ratelimit-limit: 100;w=21600
ratelimit-remaining: 0;w=21600
docker-ratelimit-source: 128.105.145.47
```

### No fallback source for a 2.x image
```
$ docker images | grep -iE 'cassandra:2'        -> NONE on host
$ for n in 4 kind nodes: crictl images | grep ' 2\.'  -> NONE
$ docker ps | grep -iE 'regist|mirror|proxy'    -> no local registry / mirror container
$ docker exec kind-worker cat /etc/containerd/config.toml | grep -i mirror -> (empty; no mirror configured)
$ cat ~/.docker/config.json                      -> no registry auth configured
```

---

## 4. Disposition: confirmed-blocked

A faithful reproduction of CASSANDRA-15970 requires, irreducibly:
1. A **cassandra:2.1** pod to author the legacy collection-range-tombstone-on-dropped-collection data
   (3.11.7 cannot write the 2.1 storage format that triggers the NPE), and
2. The specific **cassandra:3.11.7** buggy image to read/replay it.

Neither image is available in this environment:
* Not pre-pulled on any kind node and not in the host docker cache.
* Cannot be pulled now: the Docker Hub anonymous pull-rate limit for this source IP (128.105.145.47) is
  exhausted (`ratelimit-remaining: 0`, 6-hour window), and no registry mirror / pull-through cache /
  docker auth is configured to bypass it.

The official reproducer is itself an in-JVM dtest performing a 2.1->3.x upgrade (`dtest-3.0.21.jar` in the
stack frames). Without a 2.1 image to stage the legacy data, the buggy code path cannot be exercised here.

This is **confirmed-blocked** on missing infrastructure (a Cassandra 2.1 image / a way to author 2.1
legacy-format data). It is NOT "not-reproducible" — the body's mechanism is sound and 3.11.6/3.11.7 (both
< fix 3.11.8) contain the buggy legacy read path; it simply cannot be staged with the images obtainable now.

No verbatim buggy signature could be captured from a running pod (no reproduction was executed), so
"reproduced" is NOT claimed.

---

## 5. Teardown
Namespace repro-15970 was created (empty — no pods scheduled because images were unpullable) and deleted:
```
$ kubectl delete ns repro-15970 --wait=false
```
No pre-existing namespaces were touched. No repo/tooling/Cassandra files were modified (record-only).
