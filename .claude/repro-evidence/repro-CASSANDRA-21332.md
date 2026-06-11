# CASSANDRA-21332 Reproduction Log

## Bug
**Summary:** Queries on Static SAI-indexed Columns May Resurrect Range Tombstoned Data During Replica Filtering Protection
**Buggy version:** 5.0.8  (fix = 5.0.9, also 6.0-alpha2, 7.x)
**Status:** Resolved
**Category:** cql-semantics (SAI / Replica Filtering Protection), multi-node
**Control image:** NONE — fix patch 5.0.9 exceeds the 5.0->8 Docker Hub ceiling, so no fixed `cassandra:5.0.9` image exists. A/B control is impossible; use within-version reasoning.

## Primary-source reproducer (from /tmp/jira_issues/CASSANDRA-21332.json)
The reproducer is an **in-JVM dtest** added to `ReplicaFilteringWithStaticsTest`. The core mechanism:

```java
// Table: static column s1, read_repair='NONE', compaction disabled, SAI index on s1
CREATE TABLE %s.range_tombstone_with_static_sai
  (pk0 int, ck0 boolean, ck1 double, s1 int static, v0 boolean,
   PRIMARY KEY (pk0, ck0, ck1)) WITH read_repair = 'NONE';
CREATE INDEX ON %s.<table>(s1) USING 'sai';

// PER-REPLICA DIVERGENT DATA on the SAME partition key pk0=1, via executeInternal (direct local apply):
CLUSTER.get(3).executeInternal( INSERT ... (1, false, 1.0, 99, false) USING TIMESTAMP 1 );  // node3: stale row ck0=false
CLUSTER.get(1).executeInternal( INSERT ... (1, true,  4.0, 99, false) USING TIMESTAMP 1 );  // node1: stale row ck0=true
CLUSTER.get(2).executeInternal( DELETE ... USING TIMESTAMP 2 WHERE pk0=1 AND ck0 <= true ); // node2: range tombstone (covers all)
CLUSTER.get(2).executeInternal( INSERT ... (1, true,  5.0, 42, true ) USING TIMESTAMP 3 );  // node2: surviving row s1=42

// Query via coordinator with CL=ALL, page size 1:
SELECT ck0, ck1 FROM <table> WHERE s1 = 42;   // SAI path
// EXPECTED (correct): exactly one row -> (true, 5.0)
// BUG: extra rows (false,1.0) and/or (true,4.0) resurrected because the range tombstone
//      from node2 is not provided to the Replica Filtering Protection (RFP) completion read
//      for nodes 1 and 3, so the logically-deleted stale rows are not shadowed.
```

### CRITICAL staging requirement
`executeInternal` writes DIFFERENT data for the **same partition key (pk0=1)** to DIFFERENT replicas. This is a replica-divergence invariant violation that the normal coordinator/CQL write path CANNOT produce (partition-key routing + replication keep all RF replicas consistent for a given partition). The only kubectl/nodetool emulation is: isolate a node via `nodetool disablegossip` on the others, then write at `CONSISTENCY ONE` to the isolated node, flush, re-converge. This must be VERIFIED (replicas must genuinely differ) before the bug query is meaningful.

## Topology
3 pods, RF=3, namespace repro-21332, image cassandra:5.0.8.
Mapping: dtest node1->cass-0, node2->cass-1, node3->cass-2; coordinator(1) -> cass-0.

---
## Evidence

### Ring (3-node RF=3, cassandra:5.0.8), all UN
```
$ kubectl exec -n repro-21332 cass-0 -- nodetool status
UN  10.244.1.12 ... (cass-1, node2)
UN  10.244.3.13 ... (cass-2, node3)
UN  10.244.2.10 ... (cass-0, node1)
$ kubectl exec -n repro-21332 cass-0 -- nodetool version  ->  ReleaseVersion: 5.0.8
```

### Schema (matches dtest exactly)
```
CREATE KEYSPACE rfp21332 WITH replication={'class':'NetworkTopologyStrategy','dc1':3};
CREATE TABLE rfp21332.rt_static_sai (pk0 int, ck0 boolean, ck1 double, s1 int static, v0 boolean,
  PRIMARY KEY (pk0, ck0, ck1)) WITH read_repair = 'NONE';
CREATE CUSTOM INDEX ... ON rfp21332.rt_static_sai(s1) USING 'StorageAttachedIndex';
```
Prep on all 3 nodes: `nodetool disablehandoff` + `nodetool disableautocompaction`.

### Divergence primitive (replaces dtest executeInternal)
For each round: `nodetool disablegossip` on the OTHER two pods -> poll `nodetool status` until they
show **DN** from the writer's view -> `cqlsh CONSISTENCY ONE; <write> USING TIMESTAMP n` inside the
writer pod -> `nodetool flush` -> `enablegossip` on the others -> poll until UN=3.

NOTE on verification: a CL=ONE *read* is routed by the coordinator to an arbitrary replica, so it CANNOT
confirm a specific node's local state (an early CL=ONE read appeared to show the row on all nodes — a pure
read-routing artifact). The correct executeInternal-read analog is **sstabledump** on each node's local
Data.db, which bypasses the coordinator. `ls` + `sstabledump` proved the data physically lands on exactly
one node per round.

### 3-way divergence PROVEN via sstabledump (per-node raw local sstable)
All three nodes share partition key pk0=1 but hold DIFFERENT data — an invariant the normal coordinator/CQL
write path cannot produce (this is the whole staging requirement of the bug):

**cass-0 (node1):** static s1=99 @TS1; row clustering [true, 4.0] @TS1; (NO tombstone)
```
"static_block": s1=99 tstamp 1970-...000001Z
"row": clustering [ true, 4.0 ] liveness tstamp ...000001Z, v0=false
```
**cass-1 (node2):** static s1=42 @TS3; RANGE TOMBSTONE (marked_deleted @TS2, bounds inclusive..end inclusive [true,*]); surviving row [true,5.0] @TS3
```
"static_block": s1=42 tstamp ...000003Z
"range_tombstone_bound" start inclusive  deletion_info marked_deleted ...000002Z
"row": clustering [ true, 5.0 ] liveness tstamp ...000003Z, v0=true
"range_tombstone_bound" end inclusive clustering [ true, "*" ] marked_deleted ...000002Z
```
**cass-2 (node3):** static s1=99 @TS1; row clustering [false, 1.0] @TS1; (NO tombstone)
```
"static_block": s1=99 tstamp ...000001Z
"row": clustering [ false, 1.0 ] liveness tstamp ...000001Z, v0=false
```
This is exactly the dtest's per-replica setup (node3 stale ck0=false, node1 stale ck0=true, node2 has the
range tombstone covering ck0<=true plus the only surviving row s1=42).

### *** BUG QUERY — VERBATIM BUGGY SIGNATURE *** (from cass-0 = dtest coordinator(1))
```
$ kubectl exec -n repro-21332 cass-0 -- cqlsh -e "CONSISTENCY ALL; PAGING 1; SELECT ck0, ck1 FROM rfp21332.rt_static_sai WHERE s1 = 42;"
Consistency level set to ALL.
Page size: 1

 ck0   | ck1
-------+-----
 False |   1     <-- RESURRECTED (from cass-2, covered by range tombstone TS2)
 True  |   4     <-- RESURRECTED (from cass-0, covered by range tombstone TS2)
 True  |   5     <-- the ONLY row that should survive

(3 rows)
```
dtest assertion is `assertRows(..., row(true, 5.0))` -> EXACTLY ONE row expected. Observed = 3 rows.
The two extra rows (false,1.0) and (true,4.0) are range-tombstoned data RESURRECTED via the SAI + Replica
Filtering Protection (RFP) completion-read path. Root cause: the SAI first-pass query (s1=42) matches only
the surviving row on cass-1; the RFP completion reads on cass-0/cass-2 then re-read the whole partition
WITHOUT the range tombstone (which lives only on cass-1) being supplied, so the logically-deleted rows are
not shadowed.

### WITHIN-VERSION A/B CONTROL (no fixed image: fix 5.0.9 > 5.0->8 Docker Hub ceiling)
Same version, same diverged data, the *normal read path* gives the CORRECT answer — only the SAI/RFP path is wrong:

(A) Full-partition read (normal reconciliation, range tombstone applied) -> CORRECT, 1 row:
```
$ cqlsh -e "CONSISTENCY ALL; SELECT pk0,ck0,ck1,s1,v0 FROM rfp21332.rt_static_sai WHERE pk0=1;"
 pk0 | ck0  | ck1 | s1 | v0
   1 | True |   5 | 42 | True
(1 rows)
```
(B) SAI query without paging (default), CL=ALL -> STILL BUGGY, 3 rows (False/1, True/4, True/5).
(C) SAI query from a different coordinator (cass-1), CL=ALL, PAGING 1 -> deterministically BUGGY, 3 rows.

The (A)=1-row (correct) vs (B)/bug-query=3-rows (wrong) contrast, on identical data within one version, is the
definitive A/B: the StorageAttachedIndex + RFP path resurrects range-tombstoned data the normal path hides.

## DISPOSITION: reproduced
Verbatim signature: SELECT ck0,ck1 ... WHERE s1=42 returns 3 rows [(False,1),(True,4),(True,5)] instead of
the single correct row (True,5) — range-tombstoned static-SAI rows resurrected during Replica Filtering
Protection. The prior "in-JVM-dtest-only / blocked-hard" assessment is DISPROVEN: per-replica same-partition-key
divergence was stageable in kind via gossip isolation + CL=ONE writes + flush, verified physically by sstabledump.

Tooling findings: none.
