# CASSANDRA-16071 Reproduction Evidence

**Summary:** `max_compaction_flush_memory_in_mb` is interpreted as BYTES (not MB) for SASI
index builds during COMPACTION. Setting a "reasonable-looking" small MB value makes the SASI
segment-flush threshold tiny, so a compaction-time index rebuild flushes a new on-disk index
segment file for essentially every posting -> a huge number of temp files -> the merge phase
mmaps them all -> `vm.max_map_count` exhaustion -> `java.lang.OutOfMemoryError: Map failed` ->
native-memory exhaustion -> JVM crash / node restart.

- **Issue:** CASSANDRA-16071  (component: Feature/SASI)
- **Buggy version:** cassandra:3.11.7  (run inside kind, namespace repro-16071, single pod)
- **Fixed control:** cassandra:3.11.8  (fix = parse value as `1048576L * N` instead of `N`)
- **Topology:** 1 node (purely local compaction). Tag hint topology=1node CONFIRMED.
- **vm.max_map_count on kind nodes:** 65530 (Linux default) -> OOM reachable with ~64k segments.

## Root cause (source, verified in cloned cassandra-3.11.7 tree)
`src/java/org/apache/cassandra/index/sasi/conf/IndexMode.java` (3.11.7):
```java
Long maxMemMb = indexOptions.get(INDEX_MAX_FLUSH_MEMORY_OPTION) == null
        ? (long) (1073741824 * INDEX_MAX_FLUSH_DEFAULT_MULTIPLIER) // 1G default
        : Long.parseLong(indexOptions.get(INDEX_MAX_FLUSH_MEMORY_OPTION));   // <-- NO *1MB
```
`src/java/org/apache/cassandra/index/sasi/disk/PerSSTableIndexWriter.java:363` (3.11.7):
```java
// 1G for memtable and configuration for compaction
return source == OperationType.FLUSH ? 1073741824L : columnIndex.getMode().maxCompactionFlushMemoryInMb;
```
The compaction branch returns the configured number as the BYTE threshold compared against the
in-memory segment's estimated byte size. FLUSH uses a hardcoded 1GB, so ONLY compaction triggers
the bug.

In cassandra-3.11.8 (fix): IndexMode parses `1048576L * Long.parseLong(...)` and the field is
renamed `maxCompactionFlushMemoryInBytes`; PerSSTableIndexWriter returns it directly. So `'1'`
means 1 MB and produces only a handful of segments.

## Reproducer (exact steps)
Namespace repro-16071, single cassandra:3.11.7 pod (imagePullPolicy IfNotPresent; image side-loaded
into containerd because Docker Hub was rate-limiting -- NOT run via docker).

```
CREATE KEYSPACE repro16071 WITH replication={'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE repro16071.t (id int PRIMARY KEY, v text);
CREATE CUSTOM INDEX t_v_sasi ON repro16071.t (v)
  USING 'org.apache.cassandra.index.sasi.SASIIndex'
  WITH OPTIONS = {'mode':'PREFIX','max_compaction_flush_memory_in_mb':'1'};   -- 1 -> 1 BYTE on 3.11.7
```
Verified stored options:
`{'class_name':'...SASIIndex','max_compaction_flush_memory_in_mb':'1','mode':'PREFIX','target':'v'}`

Load 100,000 rows in 3 batches (COPY FROM CSV), `nodetool flush` after each (=> 3 SSTables),
then `nodetool compact repro16071 t` (OperationType.COMPACTION => uses the buggy threshold).

## Buggy behaviour observed (3.11.7) -- segment explosion
Log spammed with one segment flush per posting:
`PerSSTableIndexWriter.java:268 - Flushed index segment .../md-6-big-SI_t_v_sasi.db_<N>, took ~80 ms`
Segment number N climbed 1:1 with rows: calibration run of 10,000 rows produced exactly 10,000
temp segment files (`SI_t_v_sasi.db_0 .. _9999`). The 100k-row compaction climbed past
N=64,000 and crashed at the max_map_count limit.

## VERBATIM BUGGY SIGNATURE (3.11.7)
Highest segment reached before crash: 64429. First failure at segment 64360.
72 occurrences of `OutOfMemoryError: Map failed`, then JVM killed (exitCode 1), pod restartCount=1.

```
ERROR [SASI-General:9] 2026-06-12 05:52:24,045 PerSSTableIndexWriter.java:262 - Failed to build index segment /var/lib/cassandra/data/repro16071/t-7f347680661f11f1ad685f61e1a30048/md-6-big-SI_t_v_sasi.db_64360
org.apache.cassandra.io.FSReadError: java.io.IOException: Map failed
	at org.apache.cassandra.io.util.ChannelProxy.map(ChannelProxy.java:157) ~[apache-cassandra-3.11.7.jar:3.11.7]
	at org.apache.cassandra.index.sasi.utils.MappedBuffer.<init>(MappedBuffer.java:78) ~[apache-cassandra-3.11.7.jar:3.11.7]
	at org.apache.cassandra.index.sasi.utils.MappedBuffer.<init>(MappedBuffer.java:57) ~[apache-cassandra-3.11.7.jar:3.11.7]
	at org.apache.cassandra.index.sasi.disk.OnDiskIndex.<init>(OnDiskIndex.java:145) ~[apache-cassandra-3.11.7.jar:3.11.7]
	at org.apache.cassandra.index.sasi.disk.PerSSTableIndexWriter$Index.lambda$scheduleSegmentFlush$0(PerSSTableIndexWriter.java:258) ~[apache-cassandra-3.11.7.jar:3.11.7]
	at java.util.concurrent.FutureTask.run(FutureTask.java:266) ~[na:1.8.0_262]
	at java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1149) ~[na:1.8.0_262]
	at java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:624) ~[na:1.8.0_262]
	at org.apache.cassandra.concurrent.NamedThreadFactory.lambda$threadLocalDeallocator$0(NamedThreadFactory.java:84) ~[apache-cassandra-3.11.7.jar:3.11.7]
	at java.lang.Thread.run(Thread.java:748) ~[na:1.8.0_262]
Caused by: java.io.IOException: Map failed
	at sun.nio.ch.FileChannelImpl.map(FileChannelImpl.java:938) ~[na:1.8.0_262]
	at org.apache.cassandra.io.util.ChannelProxy.map(ChannelProxy.java:153) ~[apache-cassandra-3.11.7.jar:3.11.7]
	... 9 common frames omitted
Caused by: java.lang.OutOfMemoryError: Map failed
	at sun.nio.ch.FileChannelImpl.map0(Native Method) ~[na:1.8.0_262]
	at sun.nio.ch.FileChannelImpl.map(FileChannelImpl.java:935) ~[na:1.8.0_262]
	... 10 common frames omitted
```
Final fatal JVM error (process death -> pod restart):
```
# There is insufficient memory for the Java Runtime Environment to continue.
# Native memory allocation (malloc) failed to allocate 4088 bytes for AllocateHeap
# An error report file with more information is saved as:
# /tmp/hs_err_pid1.log
```
Pod lastState: terminated, exitCode 1, reason Error, restartCount 1.

Full pre-crash container log preserved at: /tmp/repro-16071-prev.log
Extracted OOM trace at: /tmp/repro-16071-oom-trace.txt

## A/B CONTROL (cassandra:3.11.8) -- see appended section below

## A/B CONTROL RESULT (cassandra:3.11.8, IDENTICAL workload)
Same namespace, single pod swapped to cassandra:3.11.8. Identical schema and SASI index option
`max_compaction_flush_memory_in_mb: '1'` (verified in system_schema.indexes). Identical 100,000-row
load in flushed batches, then `nodetool compact repro16071 t`.

Observed:
- Compaction COMPLETED cleanly in <15s (`pending tasks: 0`).
- Total temp segments created across the whole compaction: **50** (vs 64,000+ on 3.11.7).
- Final index = a single `md-6-big-SI_t_v_sasi.db`. No `OutOfMemoryError: Map failed`.
- 0 OOM log lines, pod restartCount=0 (no crash).
- SASI query still correct: `SELECT id FROM repro16071.t WHERE v LIKE 'val_00012345%'` -> id=12345.

A/B contrast for identical data + identical `'1'` option:
| version | meaning of '1' | temp segments | outcome |
|---------|----------------|---------------|---------|
| 3.11.7 (buggy)  | 1 byte    | ~64,000+ (1 per row) | OutOfMemoryError: Map failed -> JVM crash, pod restart |
| 3.11.8 (fixed)  | 1 MB      | 50                   | compaction completes, node healthy |

The ~1280x difference matches the missing `1048576L *` multiplier. CONFIRMED reproduced on the
buggy image with a verbatim server signature; fixed image does not misbehave.

## tag_correction
- topology hint (1node): CORRECT.
- trigger hint ("set ...=512 (treated as bytes) + large SASI compaction -> millions of temp files
  -> OOM Map Error"): CORRECT mechanism. Used '1' (instead of 512) to make 1 segment ~= 1 row so
  the OOM fires at ~64k rows (a few MB) instead of needing far more data with value 512; this is a
  size optimization of the same mechanism, not a different bug. Default vm.max_map_count=65530 on
  the kind nodes set the row target.
