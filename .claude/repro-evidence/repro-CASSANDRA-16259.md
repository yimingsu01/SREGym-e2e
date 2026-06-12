# CASSANDRA-16259 — tablehistograms cause ArrayIndexOutOfBoundsException

- **Disposition:** REPRODUCED (verbatim buggy signature + clean A/B control).
- **Buggy version:** cassandra:3.11.9
- **Fixed control:** cassandra:3.11.10 (fix released; 3.11 ceiling = 19, so control is valid)
- **Topology:** single node (1 pod) — confirms classifier hint `topology=1node`.
- **Namespace:** repro-16259  |  **Keyspace:** repro16259_ks  |  **Table:** repro16259_ks.hist_bug
- **Components:** Observability/Metrics

## Root cause (from Jira body + source + Benjamin Lerer comment)
`TableMetrics.combineHistograms` (3.11.9, line 261) aggregates the per-sstable
EstimatedHistogram for `estimatedColumnCount` (cells-per-partition). It sizes the
accumulator `values[]` from the FIRST sstable's bucket array, then for a later
sstable with FEWER buckets executes `for (i=0; i<values.length; i++) values[i] += nextBucket[i]`,
indexing `nextBucket[i]` out of bounds.

CASSANDRA-15164 (shipped in 3.11.9) raised the default CellPerPartitionCount histogram
bucket count from **114 -> 118**. So after upgrading a node from 3.11.8 to 3.11.9, a table
that holds BOTH an old 3.11.8-written sstable (115 bucket rows = 114 offsets + overflow)
and a new 3.11.9-written sstable (119 bucket rows = 118 + overflow) makes
`combineHistograms` throw `ArrayIndexOutOfBoundsException: 115` the first time the larger
histogram is accumulated before the smaller one. The reporter saw it via `nodetool
tablehistograms` and via Datastax MCAC polling JMX every 30s. "Scrubbing the affected
table makes histograms work again" (rewrites all sstables to a uniform bucket count).

## EXACT reproducer extracted (an UPGRADE scenario, not a single fresh node)
The classifier hint ("table with sstables having mismatched histogram bucket counts")
is correct on mechanism but **incomplete on how to create the mismatch**: within a single
version every sstable's cell-count histogram has the same bucket count, so a fresh 3.11.9
node CANNOT reproduce (verified — see below). The mismatch only arises across the
3.11.8 -> 3.11.9 boundary because of the 114->118 default change. Reproducer:
1. Run 3.11.8 on a persistent data dir; create table; write rows; `nodetool flush`
   -> sstable with 115 cell-count buckets. `tablehistograms` works.
2. Upgrade the SAME data dir to 3.11.9; write a new row; `nodetool flush`
   -> a second sstable with 119 buckets, coexisting with the 115-bucket one.
3. `nodetool tablehistograms repro16259_ks hist_bug` -> ArrayIndexOutOfBoundsException: 115.

## Infra
Existing kind cluster (context kind-kind, 4 nodes). Single Cassandra pod named `cass`
in ns `repro-16259`, mounting a 1Gi PVC (storageclass `standard`, local-path) at
/var/lib/cassandra so data survives the version swap. Pod image swapped in place
(3.11.8 -> 3.11.9 -> 3.11.10) on the same PVC; data identical across the swap.

============================================================
## STEP 0 — Negative check: fresh single-version 3.11.9 does NOT reproduce
Created repro16259_ks.hist_bug2 with one big partition (454826 B, ~2000 cells) +
several tiny partitions, compaction disabled, 3 sstables. tablehistograms succeeded
(EXIT=0) because all sstables had the SAME (119) bucket count. This is why a plain
fresh node is insufficient and the upgrade is required.

  $ kubectl exec -n repro-16259 cass -- nodetool tablehistograms repro16259_ks hist_bug2
  repro16259_ks/hist_bug2 histograms
  Percentile  SSTables     Write Latency      Read Latency    Partition Size        Cell Count
  ...
  Max             0.00           5839.59              0.00            454826              2299
  EXIT_CODE=0

============================================================
## STEP 1 — 3.11.8 seeds an OLD-format (115-bucket) sstable
  $ kubectl exec -n repro-16259 cass -- cqlsh -e "SELECT release_version FROM system.local;"
   release_version
   -----------------
            3.11.8

  CREATE KEYSPACE repro16259_ks WITH replication={'class':'SimpleStrategy','replication_factor':1};
  CREATE TABLE repro16259_ks.hist_bug (pk int, ck int, v text, PRIMARY KEY (pk, ck))
      WITH compaction = {'class':'SizeTieredCompactionStrategy','enabled':'false'};
  nodetool disableautocompaction repro16259_ks hist_bug
  INSERT (pk,ck,v) (1,0,'old8a'),(2,0,'old8b'),(3,0,'old8c');
  nodetool flush repro16259_ks hist_bug

  # bucket count of the 3.11.8 sstable (cell-count histogram rows):
  md-1-big-Data.db -> bucket_rows=115

## CONTROL-A — tablehistograms on 3.11.8 (pre-upgrade) WORKS
  $ kubectl exec -n repro-16259 cass -- nodetool tablehistograms repro16259_ks hist_bug
  repro16259_ks/hist_bug histograms
  Percentile  SSTables     Write Latency      Read Latency    Partition Size        Cell Count
  50%             0.00            105.78              0.00                42                 1
  ...
  Max             0.00            263.21              0.00                42                 1
  EXIT=0
  (then: nodetool drain; delete pod, keep PVC)

============================================================
## STEP 2 — Upgrade SAME data dir to 3.11.9, add a NEW-format (119-bucket) sstable
  $ kubectl exec -n repro-16259 cass -- cqlsh -e "SELECT release_version FROM system.local;"
            3.11.9
  INSERT (pk,ck,v) (10,0,'new9'),(11,0,'new9b');
  nodetool flush repro16259_ks hist_bug

  # both sstables now coexist on the upgraded node, with MISMATCHED bucket counts:
  md-1-big-Data.db -> bucket_rows=115     (written by 3.11.8)
  md-2-big-Data.db -> bucket_rows=119     (written by 3.11.9)

============================================================
## STEP 3 — REPRODUCER: tablehistograms on UPGRADED 3.11.9  ==> BUGGY SIGNATURE
  $ kubectl exec -n repro-16259 cass -- nodetool tablehistograms repro16259_ks hist_bug
  error: 115
  -- StackTrace --
  java.lang.ArrayIndexOutOfBoundsException: 115
  	at org.apache.cassandra.metrics.TableMetrics.combineHistograms(TableMetrics.java:261)
  	at org.apache.cassandra.metrics.TableMetrics.access$000(TableMetrics.java:48)
  	at org.apache.cassandra.metrics.TableMetrics$11.getValue(TableMetrics.java:376)
  	at org.apache.cassandra.metrics.TableMetrics$11.getValue(TableMetrics.java:373)
  	at org.apache.cassandra.metrics.CassandraMetricsRegistry$JmxGauge.getValue(CassandraMetricsRegistry.java:250)
  	at sun.reflect.NativeMethodAccessorImpl.invoke0(Native Method)
  	... (JMX/RMI frames) ...
  	at java.lang.Thread.run(Thread.java:748)
  command terminated with exit code 2
  EXIT=2

  Index value (115) == the smaller sstable's bucket count == proof of the off-by-bucket
  out-of-bounds in combineHistograms. The top 4 frames match the Jira body line-for-line:
    combineHistograms(TableMetrics.java:261) / access$000(:48) / $11.getValue(:376) / $11.getValue(:373).

============================================================
## CONTROL-B — FIXED 3.11.10 on the IDENTICAL upgraded data  ==> WORKS
Drained 3.11.9, deleted pod, started cassandra:3.11.10 on the SAME PVC (same two
sstables, verified unchanged: md-1=115 buckets, md-2=119 buckets).

  $ kubectl exec -n repro-16259 cass -- cqlsh -e "SELECT release_version FROM system.local;"
            3.11.10
  md-1-big-Data.db -> bucket_rows=115
  md-2-big-Data.db -> bucket_rows=119

  $ kubectl exec -n repro-16259 cass -- nodetool tablehistograms repro16259_ks hist_bug
  repro16259_ks/hist_bug histograms
  Percentile  SSTables     Write Latency      Read Latency    Partition Size        Cell Count
  50%             0.00              0.00              0.00                42                 1
  ...
  Max             0.00              0.00              0.00                42                 1
  EXIT=0

Identical workload + identical on-disk sstables: 3.11.9 throws AIOOBE:115, 3.11.10 returns
the histogram. Bug reproduced and the fix verified.

============================================================
## TEARDOWN
kubectl delete ns repro-16259 --wait=false   (PVC cass-data is deleted with the ns; local-path reclaimPolicy=Delete)
