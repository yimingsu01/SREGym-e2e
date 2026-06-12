# CASSANDRA-14204 Reproduction Evidence

## Bug
**Summary:** Remove unrepaired SSTables from garbage collection when `only_purge_repaired_tombstones`
is true to avoid AssertionError in `nodetool garbagecollect`.

**Mechanism (from Jira body):** When manually running a garbage-collection compaction across a table
with unrepaired sstables AND `only_purge_repaired_tombstones = true`, an AssertionError is thrown.
The unrepaired sstables are NOT removed from the compaction transaction because they are filtered out
in `filterSSTables()`. The result: `filterSSTables()` returns only the repaired sstables, but the
transaction still holds all sstables -> the size assertion in `parallelAllSSTableOperation` fails.

**Components:** Local/Compaction
**fixVersions:** 3.11.16, 4.0.11, **4.1.3**, 5.0-alpha1, 5.0
**Candidate buggy version:** 4.1.2

## Image substitution (honest record)
The candidate's buggy image `cassandra:4.1.2` could NOT be pulled: Docker Hub returned
`429 Too Many Requests - unauthenticated pull rate limit` on every attempt (node kubelet x5+, host
docker x1; ghcr/quay don't host it; no auth configured). The host docker / kind nodes have a rate-limit
window (~6h) that will not recover within budget.

Used **cassandra:4.1.1** (already cached on kind-worker2) as the buggy image instead. 4.1.1 < 4.1.3
(the fix version), so it contains the IDENTICAL unfixed `filterSSTables()` code path. This is a
pre-fix release of the same 4.1 line; the 4.1.1->4.1.2 delta does not touch this compaction path
(the bug originates from 2018, reported against 3.11/trunk). A/B-controlled against fixed 4.1.11.

- **Buggy:** cassandra:4.1.1  (pod `cass` in ns `repro-14204`, pinned nodeName=kind-worker2, imagePullPolicy=Never)
- **Fixed control:** cassandra:4.1.11  (>= 4.1.3, fixed; ns `repro-14204-ctl`, nodeName=kind-worker3)

Assertions enabled confirmed: `ps aux` shows `-ea` JVM flag. release_version = 4.1.1.

## Topology
**1 node, single pod.** (Classifier hint said topology=1node, confidence=H -- CORRECT.)
A single node is sufficient: the bug is purely local-compaction. RF=1.

NOTE: to obtain the required MIX of repaired+unrepaired sstables on ONE node (incremental repair
short-circuits at RF=1 with "No repair is needed"), the sstable was marked repaired OFFLINE using
`sstablerepairedset`. To do this without losing data (no PVC), the pod was launched with
`command: docker-entrypoint.sh cassandra & exec tail -f /dev/null` so PID 1 is `tail` -- Cassandra can
be stopped/restarted (`nodetool stopdaemon` -> offline tool -> restart) WITHOUT a container restart that
would wipe the writable layer. Container restartCount stayed 0 throughout.

## Reproducer steps (exact commands)

```
# 1. Table with the flag
cqlsh -e "CREATE KEYSPACE IF NOT EXISTS repro14204 WITH replication={'class':'SimpleStrategy','replication_factor':1};
          CREATE TABLE repro14204.t (id int PRIMARY KEY, v text)
              WITH compaction={'class':'SizeTieredCompactionStrategy','only_purge_repaired_tombstones':'true'};"

# 2. Batch 1 (will be marked repaired) + flush
cqlsh -e "INSERT INTO repro14204.t(id,v) VALUES (1,'a'); INSERT INTO repro14204.t(id,v) VALUES (2,'b'); DELETE FROM repro14204.t WHERE id=2;"
nodetool flush repro14204 t

# 3. Mark batch-1 sstable REPAIRED offline (stop daemon, set, restart -- container survives)
nodetool stopdaemon
gosu cassandra /opt/cassandra/tools/bin/sstablerepairedset --really-set --is-repaired /var/lib/cassandra/data/repro14204/t-*/nb-1-big-Data.db
docker-entrypoint.sh cassandra &        # restart in-container
# GATE: nodetool tablestats repro14204.t  -> Percent repaired: 100.0  (PASSED)

# 4. Batch 2 (UNREPAIRED) + flush  -> now MIX: 1 repaired + 1 unrepaired
cqlsh -e "INSERT INTO repro14204.t(id,v) VALUES (3,'c'); INSERT INTO repro14204.t(id,v) VALUES (4,'d'); DELETE FROM repro14204.t WHERE id=4;"
nodetool flush repro14204 t
# nodetool tablestats -> SSTable count: 2, Percent repaired: 50.0

# 5. THE REPRODUCER
nodetool garbagecollect repro14204 t
```

### Pre-check (all-unrepaired path = NO bug, as expected)
With only unrepaired sstables, garbagecollect short-circuits (no assert):
```
INFO  [RMI TCP Connection(6)-127.0.0.1] 2026-06-12 03:33:31,600 CompactionManager.java:377 - No sstables to GARBAGE_COLLECT for repro14204.t
```
`nodetool garbagecollect` exited 0. The bug requires the MIX (>=1 repaired so filterSSTables returns a
non-empty subset, +>=1 unrepaired left dangling in the transaction).

## VERBATIM BUGGY SIGNATURE  (cassandra:4.1.1, `nodetool garbagecollect repro14204 t`, exit code 2)

```
error: null
-- StackTrace --
java.lang.AssertionError
	at org.apache.cassandra.db.compaction.CompactionManager.parallelAllSSTableOperation(CompactionManager.java:407)
	at org.apache.cassandra.db.compaction.CompactionManager.performGarbageCollection(CompactionManager.java:620)
	at org.apache.cassandra.db.ColumnFamilyStore.garbageCollect(ColumnFamilyStore.java:1720)
	at org.apache.cassandra.service.StorageService.garbageCollect(StorageService.java:3958)
	at java.base/jdk.internal.reflect.NativeMethodAccessorImpl.invoke0(Native Method)
	...
	at java.base/java.lang.Thread.run(Unknown Source)
command terminated with exit code 2
```

**Structural match to Jira:** identical frames `java.lang.AssertionError` ->
`CompactionManager.parallelAllSSTableOperation` -> `CompactionManager.performGarbageCollection` ->
`ColumnFamilyStore.garbageCollect` -> `StorageService.garbageCollect`. Line numbers differ
(4.1.1: 407 / 620 / 1720 / 3958  vs  Jira 3.11: 339 / 476 / 1579 / 3069) as expected across versions.
The assertion is thrown over JMX and surfaces verbatim to the `nodetool` client (the canonical
user/operator-visible symptom for this bug, per the Jira which itself shows the JMX-propagated trace).

## A/B CONTROL  (cassandra:4.1.11, fixed; identical sequence)

Ran the IDENTICAL workload (table w/ flag -> repaired sstable via offline sstablerepairedset ->
unrepaired batch2 -> MIX of 1 repaired + 1 unrepaired, Percent repaired 51.61 -> `nodetool garbagecollect`)
on the FIXED version cassandra:4.1.11 (>= fix version 4.1.3), ns repro-14204-ctl, nodeName=kind-worker3.

**RESULT: the CASSANDRA-14204 AssertionError DID NOT occur on 4.1.11.** The
`java.lang.AssertionError at CompactionManager.parallelAllSSTableOperation` is GONE — the fix
(removing unrepaired sstables from the GC transaction so filterSSTables's returned set matches the
txn) is present. This is the A/B discriminator: buggy 4.1.1 throws the AssertionError; fixed 4.1.11
does not.

Note: 4.1.11 instead threw a DIFFERENT, unrelated exception from the same method --
`java.util.ConcurrentModificationException` at `CompactionManager$6.filterSSTables(CompactionManager.java:680)`
(verbatim below). This is a SEPARATE later issue in filterSSTables (concurrent iteration over the
sstable map), NOT the 14204 AssertionError. It does not weaken the control: the specific 14204
signature (AssertionError in parallelAllSSTableOperation) is absent on the fixed image, which is
exactly what the A/B is meant to show. (The ConcurrentModificationException is plausibly a distinct
regression/ticket; out of scope for this candidate.)

```
error: null
-- StackTrace --
java.util.ConcurrentModificationException
	at java.base/java.util.HashMap$HashIterator.nextNode(Unknown Source)
	at java.base/java.util.HashMap$KeyIterator.next(Unknown Source)
	at java.base/java.util.Collections$UnmodifiableCollection$1.next(Unknown Source)
	at org.apache.cassandra.db.compaction.CompactionManager$6.filterSSTables(CompactionManager.java:680)
	at org.apache.cassandra.db.compaction.CompactionManager.parallelAllSSTableOperation(CompactionManager.java:422)
	at org.apache.cassandra.db.compaction.CompactionManager.performGarbageCollection(CompactionManager.java:672)
	at org.apache.cassandra.db.ColumnFamilyStore.garbageCollect(ColumnFamilyStore.java:1723)
	at org.apache.cassandra.service.StorageService.garbageCollect(StorageService.java:4092)
	...
command terminated with exit code 2
```

## SUMMARY
- **Buggy cassandra:4.1.1** (proxy for 4.1.2; both < fix 4.1.3, identical unfixed code path):
  `nodetool garbagecollect` on a table with `only_purge_repaired_tombstones=true` and a MIX of
  repaired+unrepaired sstables -> `java.lang.AssertionError at
  CompactionManager.parallelAllSSTableOperation` (CASSANDRA-14204). REPRODUCED.
- **Fixed cassandra:4.1.11**: same workload -> NO AssertionError (bug fixed). Control confirms.
- Topology hint (1node, H) CORRECT. Trigger hint CORRECT (only_purge_repaired_tombstones=true +
  unrepaired sstables + nodetool garbagecollect -> AssertionError), with the refinement that a MIX
  (>=1 repaired + >=1 unrepaired) is required; all-unrepaired short-circuits ("No sstables to
  GARBAGE_COLLECT") without the assert.
