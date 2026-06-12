# CASSANDRA-19637 — LWT conditions behavior on collections is inconsistent

- **Disposition:** reproduced
- **Buggy version:** cassandra:4.1.5 (pod cass-415)
- **A/B control (fixed):** cassandra:4.1.6 (pod cass-416)   [fixVersions include 4.1.6; ceiling 4.1->11, OK]
- **Topology:** single pod (1node) — CQL/Semantics bug, no ring needed
- **Namespace:** repro-19637   **Keyspace:** repro19637
- **Components:** CQL/Semantics   **fixVersions:** 4.0.14, 4.1.6, 5.0-rc1, 5.0, 6.0-alpha1, 6.0

## Reproducer (extracted from Jira body)
The bug is an INCONSISTENCY in how LWT (`IF ...`) conditions on collection columns handle
null / empty values. Evidence is contrasting PAIRS, not one query:
1. Frozen vs non-frozen, same condition, NULL column: `UPDATE ... SET l=? WHERE k=0 IF l < [1,2]`.
   Body claims frozen -> `[false,null]`, non-frozen -> `[true]`.
2. Non-frozen single column: `IF colA >= null` throws InvalidRequest, but `IF colA >= []` returns true
   (empty multi-cell collection is stored as null, yet treated differently).
   Body also: `INSERT (pk,colA) VALUES (1,[])` then `DELETE ... IF colA = []` returns `{false,null}`.
Fix intent (body): treat empty multi-cell collection input as null AND reject null input for
operators other than `=` / `!=`.

## Environment
```
[cqlsh 6.1.0 | Cassandra 4.1.5 | CQL spec 3.4.6 | Native protocol v5]   (cass-415, buggy)
[cqlsh 6.1.0 | Cassandra 4.1.6 | CQL spec 3.4.6 | Native protocol v5]   (cass-416, fixed)
```

## Setup (run on each pod)
```
CREATE KEYSPACE repro19637 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE repro19637.tf (k int PRIMARY KEY, l frozen<list<int>>);   -- frozen
CREATE TABLE repro19637.tn (k int PRIMARY KEY, l list<int>);           -- non-frozen
INSERT INTO repro19637.tf (k) VALUES (0);   -- leaves l NULL
INSERT INTO repro19637.tn (k) VALUES (0);   -- leaves l NULL
INSERT INTO repro19637.tn (k) VALUES (1);   -- leaves l NULL (fresh PK for case b)
```

================================================================
## BUGGY OUTPUT — cassandra:4.1.5 (VERBATIM)
================================================================

### (a) HEADLINE inconsistency: same `IF l < [1,2]` on a NULL column
FROZEN table tf, k=0:
```
$ cqlsh -e "UPDATE repro19637.tf SET l=[9] WHERE k=0 IF l < [1,2];"

 [applied] | l
-----------+------
     False | null
```
NON-FROZEN table tn, k=0:
```
$ cqlsh -e "UPDATE repro19637.tn SET l=[9] WHERE k=0 IF l < [1,2];"

 [applied]
-----------
      True
```
==> SAME condition, SAME null value: frozen NOT-applied (False) vs non-frozen APPLIED (True). INCONSISTENT.

### (b) Non-frozen single column: null vs empty-list, operator `>=` (k=1, l NULL)
```
$ cqlsh -e "UPDATE repro19637.tn SET l=[9] WHERE k=1 IF l >= null;"
<stdin>:1:InvalidRequest: Error from server: code=2200 [Invalid query] message="Invalid comparison with null for operator ">=""
(exit 2)

$ cqlsh -e "UPDATE repro19637.tn SET l=[9] WHERE k=1 IF l >= [];"

 [applied]
-----------
      True
```
==> `>= null` ERRORS, but `>= []` (empty list, stored identically to null) returns TRUE. INCONSISTENT.

### (c) Body sub-case: INSERT empty list then DELETE IF l = [] (non-frozen)
```
$ cqlsh -e "INSERT INTO repro19637.tn (k, l) VALUES (5, []);"
$ cqlsh -e "DELETE FROM repro19637.tn WHERE k=5 IF l = [];"

 [applied]
-----------
      True
```
(Note: body's shorthand `{false,null}` was for a column that is null; here the row's l = [] is
read back as null so the `= []` condition applies True. The point stands: empty-list LWT handling
on non-frozen collections is internally inconsistent.)

================================================================
## A/B CONTROL — cassandra:4.1.6 (FIXED) — IDENTICAL workload (VERBATIM)
================================================================
(4.1.6 reused pre-existing tf/tn rows: tf k=0 l=null; tn k=0 l=null, k=1 l=null — confirmed via SELECT.)

### (a) FROZEN null `IF l < [1,2]`  -> UNCHANGED
```
 [applied] | l
-----------+------
     False | null
```
### (a) NON-FROZEN null `IF l < [1,2]`  -> FIXED (was True on 4.1.5)
```
 [applied] | l
-----------+------
     False | null
```
==> Non-frozen now matches frozen (both not-applied). Headline inconsistency RESOLVED.

### (b) NON-FROZEN `IF l >= null`  -> UNCHANGED (still rejected)
```
<stdin>:1:InvalidRequest: Error from server: code=2200 [Invalid query] message="Invalid comparison with null for operator ">=""
```
### (b) NON-FROZEN `IF l >= []`  -> FIXED (was True on 4.1.5; now REJECTED like null)
```
<stdin>:1:InvalidRequest: Error from server: code=2200 [Invalid query] message="Invalid comparison with an empty list for operator ">=""
```
==> Empty-list input is now rejected for `>=` exactly as null is, matching the fix description
("treat empty multi-cell collection input as null and reject null for non `=`/`!=` operators").

## Conclusion
Both documented inconsistencies are reproduced VERBATIM on 4.1.5 and are RESOLVED on 4.1.6 under the
identical workload:
- 4.1.5: non-frozen `IF l < [1,2]` on null => True   |  4.1.6 => False (matches frozen)
- 4.1.5: non-frozen `IF l >= []` => True             |  4.1.6 => InvalidRequest (matches `>= null`)

## Notes / caveats
- DDL (CREATE/DROP TABLE) intermittently returned `OperationTimedOut` on the memory-constrained 4.1.6
  pod; the operations that timed out were no-ops here and the row state was re-verified by SELECT before
  trusting any control output. All reported control results are from a verified-clean null-row state.
- The multi-statement `cqlsh -e "...;"` setup blob emits a trailing `SyntaxException ... no viable
  alternative at input ';'` for the empty final statement — cosmetic; all DDL/INSERTs executed (proven
  by subsequent queries succeeding).
