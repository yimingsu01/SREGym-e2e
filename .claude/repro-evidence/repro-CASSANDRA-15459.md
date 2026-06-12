# CASSANDRA-15459 — Short read protection doesn't work on group-by queries

## Bug summary (from Jira primary source)
- **Key:** CASSANDRA-15459
- **Summary:** Short read protection doesn't work on group-by queries
- **Component:** Legacy/Coordination
- **fixVersions:** 3.11.8, 4.0-beta2, 4.0  => buggy = 3.11.7, fixed-control = 3.11.8
- **Jira reproducer (verbatim from description):**
  ```
  In a two-node cluster with RF = 2
  Execute only on Node1:
    * Insert pk=1 and ck=1 with timestamp 9
    * Delete pk=0 and ck=0 with timestamp 10
    * Insert pk=2 and ck=2 with timestamp 9
  Execute only on Node2:
    * Delete pk=1 and ck=1 with timestamp 10
    * Insert pk=0 and ck=0 with timestamp 9
    * Delete pk=2 and ck=2 with timestamp 10
  Query: "SELECT pk, c FROM %s GROUP BY pk LIMIT 1"
    * Expect no live data, but got [0, 0]
  ```
- **Mechanism:** coordinator-side Short Read Protection (SRP). With per-replica divergence,
  a GROUP BY ... LIMIT query short-reads; SRP recomputes the limit using ROW count instead of
  GROUP count, so it stops early and surfaces a row (pk=0) that is actually deleted on every
  replica when properly merged. Correct result is `(0 rows)`.

## Topology decision
Ring (2-node, RF=2). Tag hint topology=ring CONFIRMED by body (real cassandra-dtest, coordinator merge).
NOT in-jvm-only, NOT single-node.

## Schema
CREATE KEYSPACE k15459 WITH replication = {'class':'SimpleStrategy','replication_factor':2};
CREATE TABLE k15459.t (pk int, c int, PRIMARY KEY (pk, c))
  WITH read_repair_chance = 0 AND dclocal_read_repair_chance = 0;

## Divergence plan (avoid hinted-handoff erasing divergence)
- Ring deployed with `hinted_handoff_enabled: false` baked into cassandra.yaml.
- Also `nodetool disablehandoff` on both pods as belt-and-suspenders.
- Isolate one peer via `nodetool disablegossip` so writes at CONSISTENCY ONE land on only one node.

## Execution log

### Cluster
2-node StatefulSet `cass` in ns `repro-15459`, image cassandra:3.11.7, RF=2.
Both UN before reproduction:
```
UN  10.244.2.101 (cass-1, HostID a837d004...)  256 tokens  100.0%
UN  10.244.1.125 (cass-0, HostID 417c72ca...)  256 tokens  100.0%
```
hinted_handoff_enabled=false confirmed in node config; `nodetool disablehandoff` also run on both (rc=0).
Table created with read_repair_chance=0.0 AND dclocal_read_repair_chance=0.0 (confirmed via DESC TABLE).

### Divergence injection (isolation dance)
Step A — isolate cass-1 (`nodetool disablegossip`); cass-0 saw cass-1 as DN. Wrote NODE1-only at CL ONE via cass-0:
  INSERT (pk=1,c=1) USING TIMESTAMP 9; DELETE (pk=0,c=0) USING TIMESTAMP 10; INSERT (pk=2,c=2) USING TIMESTAMP 9.
`nodetool flush k15459` on cass-0. sstabledump of cass-0 md-1-big-Data.db (RR-proof of NODE1 state):
```
  partition key [1] -> row clustering [1]  liveness_info tstamp 1970-01-01T00:00:00.000009Z   (LIVE @ ts9)
  partition key [0] -> row clustering [0]  deletion_info marked_deleted 1970-01-01T00:00:00.000010Z (TOMBSTONE @ ts10)
  partition key [2] -> row clustering [2]  liveness_info tstamp 1970-01-01T00:00:00.000009Z   (LIVE @ ts9)
```
cass-0 local CL ONE read: rows (1,1) and (2,2); pk=0 is a tombstone.

Step B — re-enable gossip on cass-1, isolate cass-0 (`disablegossip`); cass-1 saw cass-0 as DN. Wrote NODE2-only at CL ONE via cass-1:
  DELETE (pk=1,c=1) USING TIMESTAMP 10; INSERT (pk=0,c=0) USING TIMESTAMP 9; DELETE (pk=2,c=2) USING TIMESTAMP 10.
`nodetool flush k15459` on cass-1. sstabledump of cass-1 Data.db (RR-proof of NODE2 state):
```
  partition key [1] -> row clustering [1]  deletion_info marked_deleted 1970-01-01T00:00:00.000010Z (TOMBSTONE @ ts10)
  partition key [0] -> row clustering [0]  liveness_info tstamp 1970-01-01T00:00:00.000009Z   (LIVE @ ts9)
  partition key [2] -> row clustering [2]  deletion_info marked_deleted 1970-01-01T00:00:00.000010Z (TOMBSTONE @ ts10)
```
cass-1 local CL ONE read: row (0,0) only.

Step C — re-enable gossip on cass-0; both back to UN (divergence preserved, handoff off so no hint replay).

### Merged truth (what a correct coordinator must return)
Per-partition resolution across the two replicas (higher timestamp wins):
- pk=0: INSERT@9 (cass-1) vs DELETE@10 (cass-0) -> DELETE wins -> DEAD
- pk=1: INSERT@9 (cass-0) vs DELETE@10 (cass-1) -> DELETE wins -> DEAD
- pk=2: INSERT@9 (cass-0) vs DELETE@10 (cass-1) -> DELETE wins -> DEAD
=> All three partitions are dead. Correct result of `GROUP BY pk LIMIT 1` is (0 rows).
Corroboration: unbounded `SELECT pk,c FROM k15459.t GROUP BY pk` at CL=ALL returned **(0 rows)** (see below).

### BUGGY SIGNATURE (cassandra:3.11.7) — the money query
Command:
  kubectl exec -n repro-15459 cass-0 -- cqlsh -e "CONSISTENCY ALL; SELECT pk, c FROM k15459.t GROUP BY pk LIMIT 1"
Output (VERBATIM, first run):
```
Consistency level set to ALL.

 pk | c
----+---
  0 | 0

(1 rows)

Warnings :
Aggregation query used without partition key
```
=> Returned the DELETED row [0, 0] instead of (0 rows). Exactly matches Jira: "Expect no live data, but got [0, 0]".

Further corroboration that this is the SRP row-vs-group counting bug (not a stale read):
- Unbounded GROUP BY (no LIMIT) at CL=ALL returned (0 rows) — true merged state is empty.
- Re-running `GROUP BY pk LIMIT 1` at CL=ALL (after the first query's blocking read-repair partially
  reconciled state) returned a DIFFERENT dead row **[2, 2]** with (1 rows). The wrong row shifts as RR
  reconciles — consistent with SRP miscounting groups as rows and short-circuiting the limit, NOT a fixed
  stale value.

### A/B CONTROL (cassandra:3.11.8 = the fix version; ceiling 3.11->19, so 8 <= 19)
Tore down the 3.11.7 StatefulSet, deployed an IDENTICAL 2-node ring on cassandra:3.11.8 in the same
namespace, ran the SAME isolation dance and SAME workload. Both nodes confirmed release_version 3.11.8;
both UN; hinted_handoff_enabled=false + disablehandoff; same table with read_repair_chance=0.
Divergence re-verified identical to the buggy run:
```
cass-0 local (CL ONE): rows (1,1) and (2,2) live; pk=0 tombstone
cass-1 local (CL ONE): row (0,0) live only
```
SAME money query on fixed version:
  kubectl exec -n repro-15459 cass-0 -- cqlsh -e "CONSISTENCY ALL; SELECT pk, c FROM k15459.t GROUP BY pk LIMIT 1"
Output (VERBATIM):
```
Consistency level set to ALL.

 pk | c
----+---


(0 rows)

Warnings :
Aggregation query used without partition key
```
=> Fixed 3.11.8 returns (0 rows) — the CORRECT result. Identical workload + divergence + query;
   only the version differs. This isolates the defect to the SRP group-by counting fix in CASSANDRA-15459.

## DISPOSITION: reproduced
- Buggy 3.11.7: `SELECT pk, c FROM ks GROUP BY pk LIMIT 1` at CL=ALL over per-replica-divergent data
  returns a DELETED row ([0,0], then [2,2] post-RR) instead of (0 rows).
- Fixed 3.11.8: same workload returns (0 rows).
- Matches Jira exactly: "Expect no live data, but got [0, 0]".

## Teardown
Namespace repro-15459 deleted (kubectl delete ns repro-15459 --wait=false). Ephemeral storage only; no PVCs.


