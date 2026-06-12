# CASSANDRA-21065 — nodetool garbagecollect ConcurrentModificationException (Part 3 reproduction)
- Buggy version: cassandra:5.0.6  -> fixed 5.0.7 (control image 5.0.8 exists)
- Shape: NODETOOL-SEQUENCE (disableautocompaction + repeated flush + garbagecollect), single node.
- Root cause file: src/java/org/apache/cassandra/db/compaction/CompactionManager.java (filterSSTables iterates transaction.originals() while calling transaction.cancel() on each unrepaired sstable, mutating the set under iteration).

## Reproducer (buggy)
```sql
CREATE KEYSPACE IF NOT EXISTS k WITH replication={'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE k.t (pk int PRIMARY KEY, v text) WITH compaction =
 {'class':'UnifiedCompactionStrategy','only_purge_repaired_tombstones':'true','scaling_parameters':'L10'};
```
nodetool disableautocompaction k t, then INSERT + DELETE + nodetool flush >= 2 TIMES (to leave >=2 unrepaired sstables), then:
nodetool garbagecollect k t

## VERBATIM BUGGY SIGNATURE (5.0.6)
java.util.ConcurrentModificationException
  at java.util.Collections$UnmodifiableCollection$1.next
  at org.apache.cassandra.db.compaction.CompactionManager$6.filterSSTables(CompactionManager.java:691)
  at ...performGarbageCollection(CompactionManager.java:683)

## Control
Identical workload on fixed 5.0.8 runs clean. (>=2 unrepaired sstables required; a single sstable does not trip it.)
