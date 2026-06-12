# CASSANDRA-20086 — SAI vector search returns stale/wrong-ranked rows after vector overwrite

- **Issue**: CASSANDRA-20086 — "Use similarity score ordered iterators in SAI vector search; fix handling of updated vectors"
- **Component**: Feature/Vector Search
- **Buggy version**: cassandra:5.0.6
- **Fixed control**: cassandra:5.0.7 (fixVersions = 5.0.7, 6.0; 7 <= 5.0 ceiling 8)
- **Topology**: single node (matches classifier hint topology=1node, confidence=H)
- **Disposition**: REPRODUCED (wrong query result; A/B control clean)
- **Namespace**: repro-20086 (torn down)

## Reproducer extracted from the Jira body
The body contains the dtest `testUpdateVectorToWorseAndBetterPositions`. Block 1 is the core trigger;
it maps 1:1 to plain CQL + `nodetool flush` (SAI is GA in 5.0, `ORDER BY val ann of [...] LIMIT k` is
real CQL, vector type is native — no in-JVM harness needed):

1. CREATE TABLE (pk int, val vector<float,2>, PK(pk)); CREATE CUSTOM INDEX ... StorageAttachedIndex
2. INSERT pk0=[1.0,2.0], pk1=[1.0,3.0]
3. flush  -> old vectors land in SSTable A (its SAI index holds pk0 at exact-match [1,2])
4. INSERT pk0=[1.0,4.0]  (overwrite pk0 to a WORSE position; lives in memtable)
5. State A (pre-flush): `SELECT pk ... ORDER BY val ann of [1.0,2.0] LIMIT 1` MUST be pk=1; LIMIT 2 MUST be (1,0)
6. flush  -> SSTable B index holds pk0 at [1,4]; two index segments now
7. State B (post-flush): same asserts

Current stored values after step 4: pk0=[1,4], pk1=[1,3]. Query vector [1,2].
Euclidean: d(pk1)=|3-2|=1  <  d(pk0)=|4-2|=2  => CORRECT nearest = pk1.
Jira asserts `row(1)` for LIMIT 1. Buggy code returns pk0 because SSTable A's index still ranks pk0's
stale exact-match [1,2] best and the row is materialized/returned without re-scoring against pk0's
current value.

## Environment / versions
```
$ kubectl exec -n repro-20086 cass-buggy -- cqlsh -e "SELECT release_version FROM system.local"
 release_version
-----------------
           5.0.6
$ kubectl exec -n repro-20086 cass-fixed -- cqlsh -e "SELECT release_version FROM system.local"
 release_version
-----------------
           5.0.7
```
Pods: cass-buggy (5.0.6) and cass-fixed (5.0.7), both single-node, namespace repro-20086,
keyspace ks_clean20086, SAI index on val (vector<float,2>).

## BUGGY 5.0.6 — verbatim output (keyspace ks_clean20086)
```
[insert pk0=1,2 and pk1=1,3]
[FLUSH #1 -> SSTable A holds pk0=1,2 ; pk1=1,3]
[overwrite pk0 -> 1,4 in memtable]

>>>>>>>>>> STATE A (PRE-FLUSH #2) — 5.0.6 BUGGY <<<<<<<<<<
current values:
 pk | val
----+--------
  1 | [1, 3]
  0 | [1, 4]
(2 rows)
ANN [1.0,2.0] LIMIT 1  (CORRECT=pk1, since pk1=[1,3] closer than pk0=[1,4]):
 pk
----
  0          <-- WRONG (expected pk=1)
(1 rows)
ANN [1.0,2.0] LIMIT 2  (CORRECT=1 then 0):
 pk
----
  1
  0
(2 rows)

[FLUSH #2 -> SSTable B holds pk0=1,4 ; two index segments now]
>>>>>>>>>> STATE B (POST-FLUSH #2) — 5.0.6 BUGGY <<<<<<<<<<
ANN [1.0,2.0] LIMIT 1  (CORRECT=pk1):
 pk
----
  0          <-- WRONG (expected pk=1)
(1 rows)
ANN [1.0,2.0] LIMIT 2  (CORRECT=1 then 0):
 pk
----
  1
  0
(2 rows)
```

### MONEY SHOT (verbatim_signature)
`SELECT pk FROM t ORDER BY val ann of [1.0, 2.0] LIMIT 1` returns `pk=0` on 5.0.6, but the correct
nearest neighbor to [1,2] is pk=1 (pk1=[1,3] dist 1 < pk0=[1,4] dist 2). The Jira test asserts
`row(1)`. The bug is wrong-ranked rows from a stale overwritten vector still in an SSTable's SAI index.

Note: the LIMIT 2 result happens to look ordered (1,0) only because both rows are returned and pk1's
real score wins among them; the LIMIT 1 case exposes the defect because the stale pk0 exact-match score
beats pk1 in the index-ordered top-k, so pk0 is returned as the single nearest instead of pk1.

## CONTROL: FIXED 5.0.7 — IDENTICAL workload, verbatim output
```
>>>>>>>>>> STATE A (PRE-FLUSH #2) — 5.0.7 FIXED <<<<<<<<<<
current values:
 pk | val
----+--------
  1 | [1, 3]
  0 | [1, 4]
(2 rows)
ANN [1.0,2.0] LIMIT 1  (CORRECT=pk1):
 pk
----
  1          <-- CORRECT
(1 rows)
ANN [1.0,2.0] LIMIT 2  (CORRECT=1 then 0):
 pk
----
  1
  0
(2 rows)

>>>>>>>>>> STATE B (POST-FLUSH #2) — 5.0.7 FIXED <<<<<<<<<<
ANN [1.0,2.0] LIMIT 1  (CORRECT=pk1):
 pk
----
  1          <-- CORRECT
(1 rows)
ANN [1.0,2.0] LIMIT 2  (CORRECT=1 then 0):
 pk
----
  1
  0
(2 rows)
```

## Conclusion
- 5.0.6: `ORDER BY val ann of [1.0,2.0] LIMIT 1` -> **pk=0** (WRONG) in both pre-flush and post-flush states.
- 5.0.7: identical workload -> **pk=1** (CORRECT) in both states.
- Clean A/B: same data, same query, different result => the 5.0.6 SAI vector-search defect for updated
  vectors is reproduced exactly as the Jira test predicts. DISPOSITION = reproduced.

## Tag correction
Classifier hint accurate. topology=1node confirmed. Hint trigger ("INSERT vector + flush + overwrite to
worse/better position + SELECT ORDER BY val ann LIMIT k -> wrong ranked rows") matched the body exactly.
Only refinement: the most-telling signature is the LIMIT 1 case (returns pk=0 instead of pk=1); the
LIMIT 2 ordering looks superficially correct because both rows are returned.

## Tooling findings
cqlsh multi-statement `-e` with a leading newline (heredoc) reports
`SyntaxException: line 1:0 no viable alternative at input ';'` for the empty first statement but still
executes the remaining statements — produced confusing partial output on the first attempt. Re-ran with
one statement per `cqlsh -e` for clean, unambiguous evidence. No SREGym tooling was used in this manual
reproduction; the SREGym harness was not exercised here.
