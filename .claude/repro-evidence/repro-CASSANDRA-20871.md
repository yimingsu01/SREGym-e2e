# CASSANDRA-20871 — Reproduction Log

**Bug:** ArrayIndexOutOfBoundsException with repaired data tracking and counters
**Component:** Feature/Counters
**Buggy version:** cassandra:4.0.19 (single-node pod in kind)
**Fix versions:** 4.0.20, 4.1.11, 5.0.7, 6.0
**Namespace:** repro-20871 (kind-kind context)
**Keyspace:** ks20871
**Disposition:** confirmed-blocked

---

## 1. Primary source (JIRA JSON)

`fields.description` contained ONLY the target stack trace (no comments, no environment, no step-by-step
reproducer). Target buggy signature to be reproduced:

```
java.lang.RuntimeException: java.lang.ArrayIndexOutOfBoundsException: Index 1 out of bounds for length 0
	at org.apache.cassandra.service.StorageProxy$DroppableRunnable.run(StorageProxy.java:3132)
	...
Caused by: java.lang.ArrayIndexOutOfBoundsException: Index 1 out of bounds for length 0
	at org.apache.cassandra.utils.ByteArrayUtil.getShort(ByteArrayUtil.java:112)
	at org.apache.cassandra.db.marshal.ByteArrayAccessor.getShort(ByteArrayAccessor.java:190)
	at org.apache.cassandra.db.context.CounterContext.headerLength(CounterContext.java:175)
	at org.apache.cassandra.db.context.CounterContext.hasLegacyShards(CounterContext.java:597)
	at org.apache.cassandra.db.Digest$1.updateWithCounterContext(Digest.java:77)
	at org.apache.cassandra.db.rows.AbstractCell.digest(AbstractCell.java:150)
	at org.apache.cassandra.db.rows.ColumnData.digest(ColumnData.java:278)
```

## 2. Canonical reproducer (from the fix commit, the real authority)

Fix commit `76a7e43613e0810eefb53046254c8f48ad1adf50`. Three files: CHANGES.txt, `Digest.java` (+5 lines),
`CountersTest.java` (new test `testEmptyContext`).

The fix in `Digest.java#updateWithCounterContext`:
```java
// see super.updateWithCounterContext + CountersTest.testEmptyContext - counter context can be empty
if (accessor.isEmpty(context))
    return this;
if (CounterContext.instance().hasLegacyShards(context, accessor))   // <- AIOOBE here when context is empty
    return this;
```

The reproducer is the in-JVM distributed test `CountersTest.testEmptyContext` (3-node `Cluster.build(3)`):
- config: `repaired_data_tracking_for_partition_reads_enabled=true`, `repaired_data_tracking_for_range_reads_enabled=true`
- `CREATE TABLE t (a ascii, b ascii, c counter, d counter, PRIMARY KEY(a,b))`
- **`cluster.get(1).executeInternal("UPDATE ... c=c+1,d=d+1 ...")`**, get(2) +2, get(3) +3  -- i.e.
  UNCOORDINATED, node-local counter writes applied directly to each replica's memtable, bypassing the
  counter leader. This is what produces divergent local-only shards.
- `flush`, then `descriptor.getMetadataSerializer().mutateRepairMetadata(...)` to mark each sstable REPAIRED,
  `reloadSSTableMetadata()`.
- `coordinator.execute("select a,d from t where a='a1'", ALL/QUORUM)` -> digest over repaired counter cells
  -> empty context -> AIOOBE at `CounterContext.headerLength`.

**Key insight:** the length-0 counter context is manufactured by `executeInternal` (an in-JVM dtest API that
writes an uncoordinated, node-local counter mutation). A normal coordinated counter write (the only kind
cqlsh / nodetool can issue) routes through the counter leader -> read-modify-write -> a global, NON-EMPTY
shard. So cqlsh cannot stage the empty-context precondition.

## 3. What WAS staged in kind (single-node 4.0.19)

I staged every precondition reachable from cqlsh/nodetool/offline tools:

### 3a. Pod with the two flags injected into cassandra.yaml (RECORD-ONLY: config append at deploy, no
source/repo/tooling edits). PID 1 = sh, Cassandra in background, so the daemon can be stopped without
destroying the container.

Verified in the LIVE node Config dump (system.log, both boots):
```
repaired_data_tracking_for_partition_reads_enabled=true; repaired_data_tracking_for_range_reads_enabled=true
```
`nodetool version` -> `ReleaseVersion: 4.0.19`

### 3b. Counter table + coordinated counter writes (via cqlsh)
```
CREATE KEYSPACE ks20871 WITH replication={'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE ks20871.t (a ascii, b ascii, c counter, d counter, PRIMARY KEY(a,b));
UPDATE ks20871.t SET c=c+1,d=d+1 WHERE a='a1' AND b='b1';
UPDATE ks20871.t SET c=c+2,d=d+2 WHERE a='a1' AND b='b1';
UPDATE ks20871.t SET c=c+3,d=d+3 WHERE a='a1' AND b='b1';
```
Readback: `a1 | b1 | c=6 | d=6`  (1+2+3). `nodetool flush ks20871 t` -> sstable `nb-1-big-Data.db`.

### 3c. Marked the sstable REPAIRED OFFLINE (the `mutateRepairMetadata` analog)
Stopped the Cassandra Java process (container survived, PID1=sh), then:
```
/opt/cassandra/tools/bin/sstablerepairedset --really-set --is-repaired .../ks20871/t-*/nb-1-big-Data.db
```
sstablemetadata BEFORE: `Repaired at: 0`
sstablemetadata AFTER:  `Repaired at: 1781213395315 (06/11/2026 21:29:55)`   <- successfully marked repaired
(survived the daemon restart; re-checked after restart: still `Repaired at: 1781213395315`).

### 3d. Restarted daemon (flags still active) and ran the exact reads from the fix test
```
SELECT a,d FROM ks20871.t WHERE a='a1';     -- partition read (the test's query)
SELECT * FROM ks20871.t;                    -- range read
SELECT a,c FROM ks20871.t WHERE a='a1';
```

## 4. RESULT — reads return CLEAN, no AIOOBE

```
 a  | d            a  | b  | c | d            a  | c
----+---          ----+----+---+---          ----+---
 a1 | 6            a1 | b1 | 6 | 6            a1 | 6
(1 rows)          (1 rows)                   (1 rows)
```
TRACING ON partition read -> "Request complete" (no error), returns `a1 | 6`.

Log check: `grep -cE "ArrayIndexOutOfBounds|CounterContext.headerLength" system.log` -> **0**.
No repaired-data mismatch / no exception logged.

## 5. Why CLEAN here yet the bug is real

The repaired-data digest path (`Digest.updateWithCounterContext`) WAS exercised — that is exactly what
`repaired_data_tracking_for_partition_reads_enabled=true` triggers during the local read of a repaired
sstable, per-replica, even at RF=1. It handled the counter cell fine because the context was a proper
NON-EMPTY global shard (product of a coordinated write). The crash requires a length-0 context, which only
the in-JVM `executeInternal` uncoordinated node-local write produces.

## 6. Disposition: CONFIRMED-BLOCKED

- Un-stageable mechanism: the empty (length-0) counter context is produced ONLY by the dtest in-JVM API
  `cluster.get(N).executeInternal(<counter UPDATE>)` — an uncoordinated node-local counter mutation that
  bypasses the counter leader and yields divergent local-only shards that collapse to a zero-length context
  on merge during the repaired-data digest. cqlsh and nodetool can issue ONLY coordinated counter writes
  (counter leader -> read-modify-write -> non-empty global shard), so the empty-context precondition cannot
  be staged from outside the JVM.
- Everything else WAS staged: both repaired_data_tracking flags active in the live config; counter table with
  two counter columns; sstable successfully marked REPAIRED offline via `sstablerepairedset`; the exact
  partition/range reads run with tracking on. The digest path ran and did not crash.
- NOT "not-reproducible": the code path is not shadowed by client validation; it runs fine. The missing
  ingredient is the precondition (empty context), only producible by an in-JVM dtest internal.

## 7. A/B control

Moot. A fixed image (4.0.20) exists, but since the buggy version 4.0.19 did NOT misbehave (no buggy behavior
elicited), running 4.0.20 would show "clean vs clean" and prove nothing about the fix. No control deployed.

## 8. verbatim_signature

EMPTY — the bug was NOT reproduced. The Section-1 stack trace is the TARGET signature we could not stage,
not an observed one.

## 9. Teardown
`kubectl delete ns repro-20871 --wait=false` (only the namespace I created; no pre-existing ns touched).
