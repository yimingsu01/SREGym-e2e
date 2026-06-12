# CASSANDRA-20189 Reproduction Evidence

## Bug
**Summary:** Avoid possible consistency violations for SAI intersection queries over repaired index
matches and multiple non-indexed column matches.
**Buggy version:** cassandra:5.0.3   **Fixed in:** 5.0.4, 6.0
**Components:** Consistency/Coordination, Feature/SAI
**Classifier hint:** topology=ring, confidence=H, trigger="SAI index match (repaired) + multiple
non-indexed predicates + per-replica split row -> wrong/missing rows" — VERIFIED CORRECT against body.

## Mechanism (from JIRA body)
`FilterTree` (SAI post-filtering) is too aggressive about using *strict filtering* when (a) only
**repaired** matches are returned from the index column, and (b) there are still **multiple non-indexed
columns** that must be post-filtered. Strict filtering evaluates the non-indexed predicates per-replica
*before* coordinator reconciliation. When the column values that satisfy the predicates are split across
replicas (node1 has b=2, node2 has c=3), no single replica satisfies all predicates, so the row is
silently dropped from the result — a consistency violation (a CL=ALL read returns fewer rows than the
reconciled data contains).

## Topology / Setup
- Existing kind cluster (kind-kind, 4 nodes). Namespace: **repro-20189**. Keyspace: **repro20189**.
- 2-node Cassandra StatefulSet (cass-0, cass-1), ephemeral storage, GossipingPropertyFileSnitch.
- `hinted_handoff_enabled: false` appended to cassandra.yaml (prevents the isolated node from healing the
  split via hints — critical trap #1).
- Keyspace RF=2 (NetworkTopologyStrategy dc1:2). Table `partial_updates(k PK, a, b, c)` WITH
  `read_repair = 'NONE'`. SAI index `partial_updates_a_idx` on column `a`.

This is the real-ring translation of the in-JVM dtest `testPartialUpdatesOnNonIndexedColumnsAfterRepair`:
`coordinator(1).execute(... CL.ALL)` == cqlsh CONSISTENCY ALL; `executeInternal` (node-local write) ==
disable peer gossip + write at CL ONE through the live node.

## Reproducer commands + raw outputs (BUGGY 5.0.3)

### Ring status (both UN, RF=2, handoff off)
```
UN  10.244.1.137  119.82 KiB  16  100.0%  b0c72a65-...  rack1   (cass-0)
UN  10.244.3.115  114.63 KiB  16  100.0%  41d3898a-...  rack1   (cass-1)
Hinted handoff is not running   (both nodes)
```

### Step 1: repaired index match — INSERT (k=0,a=1) at CL ALL, TIMESTAMP 1, flush both, incremental repair
```
$ cqlsh -e "CONSISTENCY ALL; INSERT INTO repro20189.partial_updates(k, a) VALUES (0, 1) USING TIMESTAMP 1"   -> exit 0
$ nodetool flush repro20189   (both nodes)
$ nodetool repair repro20189
[...] repair options (parallelism: parallel, ... incremental: true, ...)
[...] Repair completed successfully
```
**Verify repaired-marking (trap #2) — sstablemetadata on the a=1 Data.db, BOTH nodes:**
```
node0: Repaired at: 1781250656915 (06/12/2026 07:50:56)   Pending repair: --
node1: Repaired at: 1781250656915 (06/12/2026 07:50:56)   Pending repair: --
```
repairedAt is non-zero on both replicas => the "repaired index match" precondition is satisfied.

### Step 2: split row — node-local writes via gossip isolation + CL ONE
```
# b=2 to cass-0 only:
$ (cass-1) nodetool disablegossip          # poll until cass-0 sees cass-1 = DN
$ (cass-0) cqlsh "CONSISTENCY ONE; INSERT INTO repro20189.partial_updates(k, b) VALUES (0, 2) USING TIMESTAMP 2"  -> exit 0
$ (cass-1) nodetool enablegossip
# c=3 to cass-1 only:
$ (cass-0) nodetool disablegossip          # poll until cass-1 sees cass-0 = DN
$ (cass-1) cqlsh "CONSISTENCY ONE; INSERT INTO repro20189.partial_updates(k, c) VALUES (0, 3) USING TIMESTAMP 3"  -> exit 0
$ (cass-0) nodetool enablegossip
$ nodetool flush repro20189   (both nodes)
```

### Step 3: verify the physical split (trap #3) — sstabledump per node
```
NODE0 (cass-0):  nb-1 -> {"name":"a","value":1}    nb-2 -> {"name":"b","value":2}   (no c)
NODE1 (cass-1):  nb-1 -> {"name":"a","value":1}    nb-2 -> {"name":"c","value":3}   (no b)
```
Reconciled view across replicas = (k=0, a=1, b=2, c=3). The row logically exists and satisfies all three
predicates only after coordinator reconciliation.

### Step 4: THE VIOLATION (CL ALL)
```
######## PK READ (no strict filtering) — proves the reconciled row exists ########
$ cqlsh -e "CONSISTENCY ALL; SELECT * FROM repro20189.partial_updates WHERE k = 0"
 k | a | b | c
---+---+---+---
 0 | 1 | 2 | 3
(1 rows)

######## SAI INTERSECTION FILTER QUERY — the bug ########
$ cqlsh -e "CONSISTENCY ALL; SELECT * FROM repro20189.partial_updates WHERE a = 1 AND b = 2 AND c = 3 ALLOW FILTERING"
 k | a | b | c
---+---+---+---


(0 rows)
```
=> Same row, same CL=ALL. PK path returns it; SAI intersection path returns **(0 rows)**. SILENT MISSING
ROW = consistency violation.

### Predicate-count characterization (confirms the exact title condition: "multiple non-indexed")
```
a = 1                       -> (1 rows)  [0,1,2,3]    indexed only
a = 1 AND b = 2 (FILTERING) -> (1 rows)  [0,1,2,3]    one non-indexed -> OK
a = 1 AND c = 3 (FILTERING) -> (1 rows)  [0,1,2,3]    one non-indexed -> OK
a = 1 AND b = 2 AND c = 3   -> (0 rows)               MULTIPLE non-indexed -> BUG
```
The row is dropped only when there are MULTIPLE non-indexed predicates whose satisfying values are split
across replicas — exactly matching "repaired index matches and multiple non-indexed column matches".

## VERBATIM BUGGY SIGNATURE
The filtered query at CONSISTENCY ALL returns:
```
(0 rows)
```
while the identical-CL PK read returns `0 | 1 | 2 | 3` (1 row). The absence of the row from the SAI
intersection result IS the bug signature.

## A/B CONTROL (cassandra:5.0.4 — fixed)
5.0.3 ring torn down; identical 2-node ring redeployed on **cassandra:5.0.4** (release_version 5.0.4
confirmed), same config (RF=2, read_repair=NONE, SAI on a, hinted_handoff disabled). Identical workload
re-run and identically verified:
- repairedAt non-zero on a=1 sstable: `Repaired at: 1781251120584 (06/12/2026 07:58:40)`
- physical split confirmed: NODE0 -> a=1,b=2 ; NODE1 -> a=1,c=3 (sstabledump)

```
######## FIXED 5.0.4 — PK READ (CONSISTENCY ALL) ########
 k | a | b | c
---+---+---+---
 0 | 1 | 2 | 3
(1 rows)

######## FIXED 5.0.4 — SAI INTERSECTION FILTER QUERY (CONSISTENCY ALL) ########
$ cqlsh -e "CONSISTENCY ALL; SELECT * FROM repro20189.partial_updates WHERE a = 1 AND b = 2 AND c = 3 ALLOW FILTERING"
 k | a | b | c
---+---+---+---
 0 | 1 | 2 | 3
(1 rows)
```

## CONCLUSION: REPRODUCED
| Query (CONSISTENCY ALL)                                | 5.0.3 (buggy) | 5.0.4 (fixed) |
|--------------------------------------------------------|---------------|---------------|
| SELECT * WHERE k=0  (PK read, no strict filtering)     | (0,1,2,3)     | (0,1,2,3)     |
| SELECT * WHERE a=1 AND b=2 AND c=3 ALLOW FILTERING     | **(0 rows)**  | (0,1,2,3)     |

On the buggy 5.0.3 image the SAI intersection query silently drops a row that demonstrably exists
(returned by the identical-CL PK read), violating consistency. On the fixed 5.0.4 image the identical
workload returns the row. Mechanism (repaired index match + multiple non-indexed split predicates ->
strict filtering -> per-replica pre-reconciliation drop) confirmed by the predicate-count ladder above.

## Tag correction
Classifier hint (topology=ring, confidence=H, trigger as stated) is CORRECT. No correction needed. The
in-JVM dtest in the body maps cleanly onto a real 2-node ring via gossip-isolation + CL ONE writes.

## Tooling findings
- `cqlsh -e "stmt1;\nstmt2;"` (multi-statement via newlines) raised SyntaxException on a trailing empty
  statement but had already executed the preceding statements — ran statements individually thereafter.
- sstable* tools are not on PATH in the cassandra:5.0.x image; full path /opt/cassandra/tools/bin/ works.
- Note: in this image `nodetool repair` defaults to incremental:true (sets repairedAt), so no -inc flag
  was needed; verified via sstablemetadata.

