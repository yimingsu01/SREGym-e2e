# CASSANDRA-16307 — Reproduction Evidence Log

## Bug
**Summary:** GROUP BY queries with paging can return deleted data.
**Buggy version:** cassandra:3.11.10  |  **fixVersions:** 3.11.11, 4.0-rc1, 4.0
**Components:** Consistency/Coordination
**Primary source:** /tmp/jira_repro/CASSANDRA-16307.json

## Reproducer extracted from Jira body (an in-JVM dtest)
2-node cluster, RF=2. INSERT (pk=0,ck=0) and (pk=1,ck=1) at CL=ALL (both rows on both nodes).
Then `cluster.get(1).executeInternal(DELETE pk=0 AND ck=0)` and
`cluster.get(2).executeInternal(DELETE pk=1 AND ck=1)` — node-LOCAL deletes (bypass replication),
so each node sees a DIFFERENT partition alive, but on reconciliation at CL=ALL BOTH partitions are dead.
`SELECT * FROM t GROUP BY pk` with paging size 1 at CL=ALL wrongly returns one of the (deleted) rows.
Correct result = 0 rows.

## Topology / tag check
HINT was topology=ring, confidence=H, trigger "per-replica row deletes + SELECT * GROUP BY pk paging 1
at CL=ALL returns a deleted row". This MATCHES the body exactly. No tag correction needed.
The dtest's per-node `executeInternal` local deletes were emulated on a real kind ring by gossip
isolation (the standard SREGym technique for per-replica divergence without an in-JVM dtest).

## Environment
kind cluster (context kind-kind), namespace `repro-16307`, 2-node StatefulSet `cass` (ephemeral storage),
unique keyspace `ks16307` RF=2, table `t (pk int, ck int, PRIMARY KEY (pk, ck))`. Hinted handoff DISABLED
on both nodes (so the isolated-delete divergence is not silently healed by hint replay).

## Steps + raw outputs

### 1. Ring up — `nodetool status` shows 2 UN
```
UN  10.244.2.106 (cass-0)  ... rack1
UN  10.244.3.104 (cass-1)  ... rack1
```

### 2. Disable hinted handoff on both, create schema, INSERT at CL=ALL
```
nodetool disablehandoff  (cass-0, cass-1)
CREATE KEYSPACE ks16307 WITH replication = {'class':'SimpleStrategy','replication_factor':2};
CREATE TABLE ks16307.t (pk int, ck int, PRIMARY KEY (pk, ck));
CONSISTENCY ALL;
INSERT INTO ks16307.t (pk, ck) VALUES (0, 0);
INSERT INTO ks16307.t (pk, ck) VALUES (1, 1);
SELECT * FROM ks16307.t;   ->  2 rows: (1,1) and (0,0)   [both rows on both replicas]
```

### 3. Create per-replica divergence via gossip isolation
The node that KEEPS gossip on marks the silent peer DOWN; the node whose gossip is disabled freezes its
view. So each local delete is issued on the node that currently sees its peer DOWN, guaranteeing the
mutation lands LOCAL-ONLY (CL=ONE, peer seen down => not forwarded):
- Phase: disablegossip cass-0  => cass-1 sees cass-0 DN => on cass-1: `CONSISTENCY ONE; DELETE ... WHERE pk=1 AND ck=1;`  (lands on cass-1 only)
- Phase: cass-0 enablegossip; cass-0 then sees cass-1 (still gossip-off) DN => on cass-0: `CONSISTENCY ONE; DELETE ... WHERE pk=0 AND ck=0;`  (lands on cass-0 only)
- cass-1 enablegossip; ring back to 2 UN on both (handoff disabled, no hints replay).

### 4. Verified physical divergence via sstabledump (ground truth on disk)
cass-0  (/var/lib/cassandra/data/ks16307/t-*/md-*-Data.db):
```
key [ "1" ]  liveness_info tstamp 2026-06-12T07:08:03.452782Z      <- (1,1) ALIVE
key [ "0" ]  deletion_info marked_deleted 2026-06-12T07:11:10.917006Z   <- (0,0) DELETED
```
cass-1:
```
key [ "1" ]  deletion_info marked_deleted 2026-06-12T07:09:56.636985Z   <- (1,1) DELETED
key [ "0" ]  liveness_info tstamp 2026-06-12T07:08:03.444682Z      <- (0,0) ALIVE
```
Mirror-image divergence. Both deletion timestamps (07:09:56, 07:11:10) are LATER than the insert
timestamps (07:08:03), so at CL=ALL reconciliation BOTH partitions' deletes win => CORRECT result = 0 rows.

### 5. THE BUGGY QUERY (first run, before read-repair heals) — cassandra:3.11.10
Command:
```
kubectl exec -n repro-16307 cass-0 -- cqlsh -e "CONSISTENCY ALL; PAGING 1; SELECT * FROM ks16307.t GROUP BY pk;"
```
Raw output (VERBATIM):
```
Consistency level set to ALL.
Page size: 1

 pk | ck
----+----
  0 |  0

(1 rows)

Warnings :
Aggregation query used without partition key
```
=> WRONG: returns deleted row (0,0). Correct answer is 0 rows.

### 6. Within-version contrast on the IDENTICAL diverged data, same CL=ALL (3.11.10)
```
CONSISTENCY ALL; PAGING OFF; SELECT * FROM ks16307.t GROUP BY pk;   ->  (0 rows)   CORRECT
CONSISTENCY ALL; PAGING OFF; SELECT * FROM ks16307.t;               ->  (0 rows)   CORRECT
CONSISTENCY ALL; PAGING 1;   SELECT * FROM ks16307.t GROUP BY pk;   ->  (1 rows) row (0,0)   WRONG (the bug)
```
The bug is specific to GROUP BY + paging at CL>ONE; non-paged and non-GROUP-BY paths reconcile correctly
to 0 rows. This isolates the defect to the paged GROUP BY coordination path (exactly the Jira title).

## A/B FIXED-IMAGE CONTROL — cassandra:3.11.11 (the exact fixVersion)
Same namespace `repro-16307`, buggy StatefulSet deleted then redeployed with image cassandra:3.11.11
(verified `release_version 3.11.11`). IDENTICAL procedure: handoff disabled, INSERT (0,0)+(1,1) at CL=ALL,
mirror divergence created by the same two gossip-isolation phases (each delete issued on the node that was
CONFIRMED to see its peer DOWN before issuing).

Mirror divergence verified on disk (sstabledump):
```
cass-0:  key [ "1" ] liveness_info tstamp 2026-06-12T07:18:11.606574Z      (1,1) ALIVE
         key [ "0" ] deletion_info marked_deleted 2026-06-12T07:18:16.813994Z   (0,0) DELETED
cass-1:  key [ "1" ] deletion_info marked_deleted 2026-06-12T07:18:32.341830Z   (1,1) DELETED
         key [ "0" ] liveness_info tstamp 2026-06-12T07:18:11.601566Z      (0,0) ALIVE
```
Same mirror-image divergence as the buggy run; deletes win on reconciliation => correct = 0 rows.

THE SAME QUERY on the FIXED image (first run):
```
kubectl exec -n repro-16307 cass-0 -- cqlsh -e "CONSISTENCY ALL; PAGING 1; SELECT * FROM ks16307.t GROUP BY pk;"
```
Raw output (VERBATIM):
```
Consistency level set to ALL.
Page size: 1

 pk | ck
----+----


(0 rows)

Warnings :
Aggregation query used without partition key
```
=> CORRECT: 0 rows.

## CONCLUSION — A/B
| version       | CONSISTENCY ALL; PAGING 1; SELECT * FROM t GROUP BY pk | verdict  |
|---------------|--------------------------------------------------------|----------|
| 3.11.10 buggy | returns 1 row: (0, 0)  [a DELETED row]                 | WRONG    |
| 3.11.11 fixed | returns 0 rows                                         | CORRECT  |

DISPOSITION: **reproduced**. Verbatim buggy signature (the spurious deleted row at CL=ALL/PAGING 1/GROUP BY):
the `0 | 0` data row followed by `(1 rows)` from the 3.11.10 buggy cqlsh output in section 5.
Topology = ring (RF=2), matches the hint; no tag correction needed. The dtest's per-node `executeInternal`
local deletes were faithfully emulated on a real kind ring via gossip isolation (with hinted handoff
disabled so the divergence is not silently healed by hint replay) and confirmed physically with
sstabledump on each node before the query.

