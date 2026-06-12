# CASSANDRA-16671 Reproduction Evidence

**Summary (Jira):** "Cassandra can return no row when the row columns have been deleted."
**Buggy version:** cassandra:3.11.10  | **Fixed-control:** cassandra:3.11.11 (fixVersions include 3.11.11)
**Components:** Legacy/Local Write-Read Paths
**Topology:** 2-node ring (matches classifier hint topology=ring). Tag correction: none.

## Bug mechanism (from Jira body, ground truth)
CQL semantics: a row exists as long as it has one non-null column (incl. PK columns).
INSERT sets the row's primary-key *liveness*; UPDATE does NOT. CASSANDRA-16226 introduced a
regression: the timestamp-ordered read path stops EARLY if an UPDATE covering all columns is
found in an SSTable. The row returned then carries no PK liveness; if another replica returns a
column DELETION, the coordinator drops the row entirely (returns 0 rows) instead of returning
`row(pk, ck, null)`.

## Exact reproducer (adapted from the Jira in-JVM dtest to a real kind ring)
Original dtest uses `executeInternal` (per-node local writes) + per-node `flush` on a 2-node
in-JVM cluster, then `coordinator(2).execute(SELECT..., ConsistencyLevel.ALL)`.
On a real ring this is reproduced by GOSSIP-ISOLATING the two pods so each write lands on exactly
one replica (clean bidirectional DN), matching the dtest's data split:
- node1 (cass-0): INSERT (USING TIMESTAMP 1000) -> flush -> UPDATE SET v (USING TIMESTAMP 2000) -> flush  (two sstables)
- node2 (cass-1): DELETE v (USING TIMESTAMP 3000) -> flush
- read from coordinator cass-1 at CONSISTENCY ALL.

## Environment
- kind cluster (context kind-kind), namespace `repro-16671`, keyspace `ks16671`.
- 2-node StatefulSet ring, image cassandra:3.11.10, ephemeral storage.
- Schema: `CREATE TABLE ks16671.tbl (pk int, ck text, v int, PRIMARY KEY (pk, ck));` RF=2 SimpleStrategy.

## Step 1 — ring up, schema created while both UN
```
$ kubectl exec -n repro-16671 cass-0 -- nodetool status
UN  10.244.3.109 ... (cass-1)
UN  10.244.2.111 ... (cass-0)
CREATE KEYSPACE ks16671 WITH replication={'class':'SimpleStrategy','replication_factor':2};
CREATE TABLE ks16671.tbl (pk int, ck text, v int, PRIMARY KEY (pk, ck));   # verified on both nodes
```

## Step 2 — gossip isolation (clean bidirectional DN)
disablehandoff + disablegossip on BOTH nodes. NOTE: `disablegossip` freezes a node's own
failure-detector evaluation, so cass-0 would never convict cass-1 while its own gossip is off.
Fix: re-enable gossip on cass-0 only (cass-1 silent) so cass-0's FD convicts cass-1. Final state
before isolated writes:
```
cass-0 view:  DN 10.244.3.109 (cass-1)   UN 10.244.2.111 (cass-0)
cass-1 view:  UN 10.244.3.109 (cass-1)   DN 10.244.2.111 (cass-0)
both gossip = not running (cass-0 re-enabled briefly only to convict, then writes done with each seeing peer DN)
```

## Step 3 — isolated divergent writes (each CL=ONE, local-only, handoff disabled)
```
cass-0:  INSERT INTO ks16671.tbl (pk,ck,v) VALUES (1,'1',1) USING TIMESTAMP 1000;  nodetool flush ks16671
cass-0:  UPDATE ks16671.tbl USING TIMESTAMP 2000 SET v=2 WHERE pk=1 AND ck='1';    nodetool flush ks16671
cass-1:  DELETE v FROM ks16671.tbl USING TIMESTAMP 3000 WHERE pk=1 AND ck='1';     nodetool flush ks16671
sstable counts: cass-0 = 2 sstables, cass-1 = 1 sstable
```

## Step 4 — PROOF of physical isolation via sstabledump (/opt/cassandra/tools/bin/sstabledump)
### cass-0 md-1-big-Data.db (the INSERT — HAS primary-key liveness):
```
"clustering":["1"],
"liveness_info":{"tstamp":"1970-01-01T00:00:00.001Z"},   <-- PK liveness @ ts1000
"cells":[ {"name":"v","value":1} ]
```
### cass-0 md-2-big-Data.db (the UPDATE covering all cols — NO liveness_info):
```
"clustering":["1"],
"cells":[ {"name":"v","value":2,"tstamp":"1970-01-01T00:00:00.002Z"} ]   <-- v=2 @ ts2000, NO row liveness
```
### cass-1 md-1-big-Data.db (ONLY the column DELETE tombstone, no live v cell):
```
"clustering":["1"],
"cells":[ {"name":"v","deletion_info":{"local_delete_time":"2026-06-12T07:34:59Z"},"tstamp":"1970-01-01T00:00:00.003Z"} ]
```
Isolation is clean: cass-1 has NO v=1/v=2 live cell; cass-0 has NO tombstone. Matches the dtest exactly.

## Step 5 — re-enable gossip on cass-1, both UN, then BUGGY READ (coordinator cass-1, CL=ALL, FIRST read)
### >>> BUGGY SIGNATURE (cassandra:3.11.10) <<<
```
$ kubectl exec -n repro-16671 cass-1 -- cqlsh -e "CONSISTENCY ALL; SELECT * FROM ks16671.tbl WHERE pk=1 AND ck='1';"
Consistency level set to ALL.

 pk | ck | v
----+----+---


(0 rows)
```
Expected per CQL semantics & the Jira dtest assertion `row(1, "1", null)`. The row is WRONGLY DROPPED.

Contrast — SELECT of the single column behaves correctly (proves the row data is present, only `SELECT *` regresses):
```
$ kubectl exec -n repro-16671 cass-1 -- cqlsh -e "CONSISTENCY ALL; SELECT v FROM ks16671.tbl WHERE pk=1 AND ck='1';"
Consistency level set to ALL.

 v
------
 null

(1 rows)
```
So `SELECT *` => 0 rows (BUG) while `SELECT v` => 1 row (null). This is exactly CASSANDRA-16671.

## Step 6 — A/B CONTROL on fixed image cassandra:3.11.11 (identical workload)
Same namespace, same keyspace/schema, same 2-node ring, same gossip-isolation, same writes.
Physical isolation verified identical via sstabledump (sstable format me- vs md-, identical content):
- cass-0 me-1: liveness_info tstamp .001Z + v=1   (INSERT, PK liveness)
- cass-0 me-2: v=2 @.002Z, NO liveness_info        (UPDATE covering all cols)
- cass-1     : v deletion_info (tombstone) @.003Z, no live v cell  (column DELETE)

### >>> CONTROL SIGNATURE (cassandra:3.11.11, FIXED) — coordinator cass-1, CL=ALL, FIRST read <<<
```
$ kubectl exec -n repro-16671 cass-1 -- cqlsh -e "CONSISTENCY ALL; SELECT * FROM ks16671.tbl WHERE pk=1 AND ck='1';"
Consistency level set to ALL.

 pk | ck | v
----+----+------
  1 |  1 | null

(1 rows)
```
```
$ kubectl exec -n repro-16671 cass-1 -- cqlsh -e "CONSISTENCY ALL; SELECT v FROM ks16671.tbl WHERE pk=1 AND ck='1';"
Consistency level set to ALL.

 v
------
 null

(1 rows)
```

## VERDICT: REPRODUCED
| Query (CL=ALL, coord cass-1)        | 3.11.10 BUGGY        | 3.11.11 FIXED        |
|-------------------------------------|----------------------|----------------------|
| SELECT * FROM tbl WHERE pk=1,ck='1' | (0 rows)  <-- BUG    | 1 | 1 | null (1 rows) |
| SELECT v FROM tbl WHERE pk=1,ck='1' | null (1 rows)        | null (1 rows)        |

The buggy 3.11.10 WRONGLY returns 0 rows for `SELECT *` (row dropped) while `SELECT v` returns 1 row,
exactly the CASSANDRA-16671 regression. The fixed 3.11.11 correctly returns `row(1, '1', null)`.
Identical physical sstable state on both versions (sstabledump-verified) isolates the difference to the
read-path fix. Topology=ring confirmed (matches hint). Tag correction: none.
