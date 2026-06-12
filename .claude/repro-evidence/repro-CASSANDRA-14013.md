# CASSANDRA-14013 — Keyspace named "snapshots" is empty after service restart

- **Buggy version:** cassandra:4.1.0
- **Fixed control:** cassandra:4.1.1 (fix landed in 4.0.8 / 4.1.1 / 5.0)
- **Topology:** 1 node (single pod)
- **Namespace:** repro-14013
- **Disposition:** REPRODUCED
- **Components:** Legacy/Core, Local/Snapshots

## Bug mechanism (from Jira body)
Reporter observes data loss in a keyspace literally named `snapshots` after restarting Cassandra.
Rows inserted into `snapshots.test_idx` are gone after restart. Happens "most" attempts, not every time
(memtable/commitlog timing). Root cause: the path component `snapshots` collides with the snapshot
directory skip-logic during the SSTable scan on startup — the keyspace's live SSTables are mistaken for
snapshot data and NOT loaded. The schema (in system_schema) is unaffected, so the table still "exists"
but appears empty.

## Reproducer (exact, from body)
```
CREATE KEYSPACE snapshots WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};
CREATE TABLE snapshots.test_idx (key text, seqno bigint, primary key(key));
INSERT INTO snapshots.test_idx (key,seqno) values ('key1', 1); ... ('key1000', 1000);
SELECT count(*) FROM snapshots.test_idx;   -> 1000
# restart service (kill cassandra-pid; cassandra -f)
SELECT count(*) FROM snapshots.test_idx;   -> 0
```
Keyspace name MUST be exactly `snapshots` (name-triggered). Used 20 rows instead of 1000 (enough).

## Method notes (decisive for validity)
- emptyDir volume mounted at /var/lib/cassandra so data survives an IN-PLACE container restart.
- "Restart" = `kubectl exec ... -- kill 1` (kills PID 1 -> container restarts in pod, emptyDir persists).
  NEVER `kubectl delete pod` (that would wipe emptyDir and give a false positive in BOTH versions).
- `nodetool flush snapshots` before restart -> rows forced to on-disk SSTables AND commitlog discarded,
  so commitlog replay cannot mask the load bug. Makes it a deterministic single-shot.

================================================================
## BUGGY 4.1.0 — RUN

### Pre-restart: 20 rows inserted + flushed
```
$ kubectl exec -n repro-14013 cass -- nodetool flush snapshots
$ kubectl exec -n repro-14013 cass -- cqlsh -e "SELECT count(*) FROM snapshots.test_idx;"
 count
-------
    20

(1 rows)
```

### On-disk SSTables present after flush (pre-restart)
```
$ kubectl exec -n repro-14013 cass -- ls -la /var/lib/cassandra/data/snapshots/test_idx-*/
-rw-r--r-- 1 cassandra cassandra   47 Jun 12 03:00 nb-1-big-CompressionInfo.db
-rw-r--r-- 1 cassandra cassandra  319 Jun 12 03:00 nb-1-big-Data.db
-rw-r--r-- 1 cassandra cassandra   40 Jun 12 03:00 nb-1-big-Filter.db
-rw-r--r-- 1 cassandra cassandra  187 Jun 12 03:00 nb-1-big-Index.db
-rw-r--r-- 1 cassandra cassandra 4778 Jun 12 03:00 nb-1-big-Statistics.db
-rw-r--r-- 1 cassandra cassandra   58 Jun 12 03:00 nb-1-big-Summary.db
```

### Restart in place (kill PID 1) -> restartCount 0 -> 1, container ready

### Post-restart: schema SURVIVES
```
$ kubectl exec -n repro-14013 cass -- cqlsh -e "DESCRIBE KEYSPACE snapshots;"
CREATE KEYSPACE snapshots WITH replication = {'class': 'SimpleStrategy', 'replication_factor': '1'}  AND durable_writes = true;
CREATE TABLE snapshots.test_idx (
    key text PRIMARY KEY,
    seqno bigint
) WITH ... ;
```

### *** VERBATIM BUGGY SIGNATURE *** — count after restart = 0
```
$ kubectl exec -n repro-14013 cass -- cqlsh -e "SELECT count(*) FROM snapshots.test_idx;"

 count
-------
     0

(1 rows)

Warnings :
Aggregation query used without partition key
```

### SSTables STILL ON DISK post-restart (proves LOAD/skip bug, not deletion)
```
$ kubectl exec -n repro-14013 cass -- ls -la /var/lib/cassandra/data/snapshots/test_idx-*/
-rw-r--r-- 1 cassandra cassandra  319 Jun 12 03:00 nb-1-big-Data.db   <-- same file, same 03:00 timestamp
... (all 6 *.db files unchanged) ...
```
Data physically intact on disk, but query returns 0 -> Cassandra skipped loading the `snapshots`-named
keyspace's live SSTables on startup (mistook the dir for a snapshot).

================================================================
## FIXED 4.1.1 — A/B CONTROL (identical workload + identical restart method)

### Pre-restart: 20 rows + flush
```
$ kubectl exec -n repro-14013 cass-fixed -- cqlsh -e "SELECT count(*) FROM snapshots.test_idx;"
 count
-------
    20
(1 rows)
```

### Restart in place (kill PID 1) -> restartCount 0 -> 1, ready

### Post-restart: data RETAINED (count stays 20)
```
$ kubectl exec -n repro-14013 cass-fixed -- cqlsh -e "SELECT count(*) FROM snapshots.test_idx;"

 count
-------
    20

(1 rows)

Warnings :
Aggregation query used without partition key
```

================================================================
## CONCLUSION
| Image          | pre-restart count | post-restart count |
|----------------|-------------------|--------------------|
| 4.1.0 (buggy)  | 20                | **0**              |
| 4.1.1 (fixed)  | 20                | **20**             |

Identical reproducer + identical in-place restart method (emptyDir preserved). Loss occurs ONLY on 4.1.0,
confirming the restart method is not the cause. The keyspace named `snapshots` loses all row data on
restart on 4.1.0 (SSTables remain on disk but are skipped at load); fixed in 4.1.1. REPRODUCED.

## Tag correction
Classifier hint (topology=1node, confidence=H, trigger=CREATE KEYSPACE snapshots + insert + restart ->
table empty) is ACCURATE. No correction needed.
