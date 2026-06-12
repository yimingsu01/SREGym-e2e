# CASSANDRA-20313 — Reproduction Evidence

**Summary (Jira):** SAI should avoid attempting to index empty values for numerics and types that do not allow them.
**Components:** Feature/SAI
**Buggy version:** 5.0.3   **Fixed version (control):** 5.0.4  (fixVersions: 5.0.4, 6.0-alpha1, 6.0)
**Disposition:** REPRODUCED (verbatim NPE in async index build, plus clean A/B contrast on 5.0.4)

## Classifier hint vs. body
- Hint: topology=1node, confidence=H, trigger="INSERT empty bytes into int column + flush + CREATE SAI INDEX -> NPE, index build fails".
- Body (ground truth) matches exactly. The Jira reproducer is an in-JVM test:
  ```
  createTable("CREATE TABLE %s (k int PRIMARY KEY, v int)");
  execute("INSERT INTO %s (k, v) VALUES (0, ?)", EMPTY_BYTE_BUFFER);
  flush();
  createIndex(... 'v' ...);  // fails!!!
  ```
- tag_correction: none. Topology=1node confirmed correct; the failing path is purely local
  (StorageAttachedIndexBuilder.indexSSTable). No ring needed.
- CQL-surface equivalent of `EMPTY_BYTE_BUFFER` into a fixed-length int column: `blobAsInt(0x)`.
  Int32Type.validate() permits remaining()==0, so an empty serialized int is legal at the storage
  layer — which is precisely why this bug exists.

## Environment
- Existing kind cluster, context kind-kind, namespace `repro-20313` (created by me).
- Buggy pod: `cass` = cassandra:5.0.3. Control pod: `cass-ctl` = cassandra:5.0.4. Both single-node.
- Keyspace `repro_20313`, table `t (k int PRIMARY KEY, v int)`.
- Verified versions via `SELECT release_version FROM system.local`: cass=5.0.3, cass-ctl=5.0.4.

## Reproducer commands (buggy node `cass`, 5.0.3)
```
CREATE KEYSPACE IF NOT EXISTS repro_20313 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE repro_20313.t (k int PRIMARY KEY, v int);
INSERT INTO repro_20313.t (k, v) VALUES (0, blobAsInt(0x));   -- empty bytes into int column
nodetool flush repro_20313 t                                  -- empty value must be in an SSTable
CREATE INDEX t_v_idx ON repro_20313.t (v) USING 'sai';        -- TRIGGER
```

### Confirm the empty value landed (NOT null — empty bytes)
```
SELECT k, v, blobAsText(intAsBlob(v)) AS vblob FROM repro_20313.t;
 k | v | vblob
---+---+-------
 0 |   |
(1 rows)
```
(v renders blank and its blob is empty -> empty buffer, exactly EMPTY_BYTE_BUFFER.)

### SSTable present after flush (so indexSSTable path is exercised)
```
ls /var/lib/cassandra/data/repro_20313/t-*/ | grep data.db
-rw-r--r-- 1 cassandra cassandra   32 ... nb-1-big-Data.db
```

### CREATE INDEX result on 5.0.3
`CREATE INDEX ... USING 'sai'` does NOT return success — cqlsh blocks waiting for the index to
become queryable and times out (the async build never completes):
```
<stdin>:1:OperationTimedOut: errors={'127.0.0.1:9042': 'Client request timeout. ...'}
command terminated with exit code 2
```

## VERBATIM BUGGY SIGNATURE (from pod log of `cass`, 5.0.3)
`kubectl logs -n repro-20313 cass | grep -A40 "Index build of"`
```
WARN  [SecondaryIndexManagement:1] 2026-06-12 03:25:06,063 SecondaryIndexManager.java:843 - Index build of t_v_idx failed. Please run full index rebuild to fix it.
java.util.concurrent.ExecutionException: java.lang.NullPointerException: Cannot invoke "org.apache.cassandra.utils.bytecomparable.ByteSource.next()" because "key" is null
	at org.apache.cassandra.utils.concurrent.AbstractFuture.getWhenDone(AbstractFuture.java:239)
	at org.apache.cassandra.utils.concurrent.AbstractFuture.get(AbstractFuture.java:246)
	at org.apache.cassandra.index.sai.StorageAttachedIndex.lambda$getInitializationTask$4(StorageAttachedIndex.java:337)
	at org.apache.cassandra.concurrent.FutureTask.call(FutureTask.java:61)
	at org.apache.cassandra.concurrent.FutureTask.run(FutureTask.java:71)
	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(Unknown Source)
	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(Unknown Source)
	at io.netty.util.concurrent.FastThreadLocalRunnable.run(FastThreadLocalRunnable.java:30)
	at java.base/java.lang.Thread.run(Unknown Source)
Caused by: java.lang.NullPointerException: Cannot invoke "org.apache.cassandra.utils.bytecomparable.ByteSource.next()" because "key" is null
	at org.apache.cassandra.db.tries.InMemoryTrie.putRecursive(InMemoryTrie.java:904)
	at org.apache.cassandra.db.tries.InMemoryTrie.putRecursive(InMemoryTrie.java:897)
	at org.apache.cassandra.db.tries.InMemoryTrie.putSingleton(InMemoryTrie.java:878)
	at org.apache.cassandra.index.sai.disk.v1.segment.SegmentTrieBuffer.add(SegmentTrieBuffer.java:69)
	at org.apache.cassandra.index.sai.disk.v1.segment.SegmentBuilder$TrieSegmentBuilder.addInternal(SegmentBuilder.java:90)
	at org.apache.cassandra.index.sai.disk.v1.segment.SegmentBuilder.add(SegmentBuilder.java:195)
	at org.apache.cassandra.index.sai.disk.v1.SSTableIndexWriter.addTerm(SSTableIndexWriter.java:208)
	at org.apache.cassandra.index.sai.disk.v1.SSTableIndexWriter.addRow(SSTableIndexWriter.java:99)
	at org.apache.cassandra.index.sai.disk.StorageAttachedIndexWriter.addRow(StorageAttachedIndexWriter.java:257)
	at org.apache.cassandra.index.sai.disk.StorageAttachedIndexWriter.nextUnfilteredCluster(StorageAttachedIndexWriter.java:131)
	at org.apache.cassandra.index.sai.StorageAttachedIndexBuilder.indexSSTable(StorageAttachedIndexBuilder.java:188)
	at org.apache.cassandra.index.sai.StorageAttachedIndexBuilder.build(StorageAttachedIndexBuilder.java:118)
	at org.apache.cassandra.db.compaction.CompactionManager$13.run(CompactionManager.java:1905)
	at org.apache.cassandra.concurrent.FutureTask$3.call(FutureTask.java:141)
	... 6 common frames omitted
```
This matches the Jira stack trace frame-for-frame (SecondaryIndexManager.java:843;
InMemoryTrie.putRecursive:904/897; putSingleton:878; SegmentTrieBuffer.add:69;
SegmentBuilder.add:195; SSTableIndexWriter.addTerm:208 / addRow:99; StorageAttachedIndexWriter
addRow:257 / nextUnfilteredCluster:131; StorageAttachedIndexBuilder.indexSSTable:188 / build:118;
CompactionManager$13.run:1905). Released 5.0.3 includes the more descriptive NPE message
("Cannot invoke ... ByteSource.next() ... because key is null"). Count of failure lines in log = 1.

## A/B CONTROL (fixed node `cass-ctl`, 5.0.4) — identical workload
Same CREATE KEYSPACE/TABLE/INSERT(blobAsInt(0x))/flush. Confirmed identical input landed:
```
SELECT k, v, blobAsText(intAsBlob(v)) FROM repro_20313.t;  ->  k=0, v empty, vblob empty
```
`CREATE INDEX t_v_idx ON repro_20313.t (v) USING 'sai';`  -> returns **EXIT 0** (immediate success).
Control pod log:
```
INFO  [SecondaryIndexManagement:1] StorageAttachedIndex.java:874 - [repro_20313.t.t_v_idx] Submitting 1 parallel initial index builds over 1 total sstables...
INFO  [SecondaryIndexExecutor:1]   V1OnDiskFormat.java:161 - [repro_20313.t.t_v_idx] Starting a compaction index build. ...
INFO  [SecondaryIndexManagement:1] SecondaryIndexManager.java:1860 - Index [t_v_idx] became queryable after successful build.
```
`grep "Index build of" cass-ctl log` -> EMPTY (no failure).
`SELECT k FROM repro_20313.t WHERE v = blobAsInt(0x);` -> 0 rows (5.0.4 correctly SKIPS indexing the
empty numeric value, exactly the fix described by the issue title), no error.

## Conclusion
On buggy 5.0.3 the SAI index build over an SSTable containing an empty value for an `int` column
throws NPE (key null in InMemoryTrie.putRecursive) and the index fails to build / is left not
queryable. On fixed 5.0.4 the identical workload builds the index successfully and the index is
queryable, with the empty value skipped. Clean reproduction with verbatim signature + A/B control.

## Teardown
`kubectl delete ns repro-20313 --wait=false` (see structured result torn_down).
