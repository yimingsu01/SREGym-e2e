# CASSANDRA-17913 — Nested selection of reversed collections fails

- **Disposition:** reproduced
- **Buggy version:** cassandra:4.1.1
- **Fixed control:** cassandra:4.1.2 (fix landed in 4.1.2 per fixVersions; within 4.1 ceiling of 11)
- **Topology:** single node (1 pod per version). Tag hint topology=1node, confidence=H — CORRECT.
- **tag_correction:** none — Jira body matches the classifier hint exactly.
- **Namespace:** repro-17913   **Keyspace:** repro17913_ks
- **Date:** 2026-06-11

## Bug summary (from Jira body — ground truth)
Element selection (`c['key']`) and slice/range selection (`c['a'..'z']`) on a **frozen collection
clustering column declared with CLUSTERING ORDER BY (c DESC)** fail with
`InvalidRequestException: ... is not a collection`. The real type is `ReversedType(MapType(...))`,
so the collection-type checks in `Selectable.WithElementSelection` / `Selectable.WithSliceSelection`
do not unwrap the ReversedType and wrongly conclude the column is not a collection. The error fires
during statement preparation — no data and no WHERE clause are required. Introduced by CASSANDRA-7396
(4.0). Fixed in 4.0.10 / 4.1.2 / 5.0.

## Reproducer (minimal, from the unit test in the body)
```
CREATE KEYSPACE repro17913_ks WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE repro17913_ks.t (k int, c frozen<map<text,int>>, v int, PRIMARY KEY(k,c))
  WITH CLUSTERING ORDER BY (c DESC);
SELECT c['testing'] FROM repro17913_ks.t;
```

## Environment / deploy
Existing kind cluster (context kind-kind, 4 nodes). Two pods deployed in parallel in namespace
repro-17913: `cass-411` (cassandra:4.1.1, buggy) and `cass-412` (cassandra:4.1.2, fixed). Single-pod
template from the skill (MAX_HEAP_SIZE=1024M, HEAP_NEWSIZE=256M, GossipingPropertyFileSnitch).

Confirmed versions:
```
$ kubectl exec -n repro-17913 cass-411 -- cqlsh -e "SHOW VERSION"
[cqlsh 6.1.0 | Cassandra 4.1.1 | CQL spec 3.4.6 | Native protocol v5]
$ kubectl exec -n repro-17913 cass-412 -- cqlsh -e "SHOW VERSION"
[cqlsh 6.1.0 | Cassandra 4.1.2 | CQL spec 3.4.6 | Native protocol v5]
```

## BUGGY signature — cassandra:4.1.1 (pod cass-411)
CREATE KEYSPACE and CREATE TABLE both succeed (no output). The element-selection SELECT throws:
```
$ kubectl exec -n repro-17913 cass-411 -- cqlsh -e "SELECT c['testing'] FROM repro17913_ks.t;"
<stdin>:1:InvalidRequest: Error from server: code=2200 [Invalid query] message="Invalid element selection: c is of type frozen<map<text, int>> is not a collection"
command terminated with exit code 2   (EXIT_RC=2)
```
This matches the Jira body's expected error verbatim (`... is not a collection`).

## A/B CONTROL — cassandra:4.1.2 (pod cass-412)
IDENTICAL workload (same keyspace/table DDL + same SELECT). The SELECT returns 0 rows, NO error:
```
$ kubectl exec -n repro-17913 cass-412 -- cqlsh -e "SELECT c['testing'] FROM repro17913_ks.t;"

 c['testing']
--------------


(0 rows)
EXIT_RC=0
```
Exception (4.1.1) vs clean 0-rows (4.1.2) on the same query = the fix.

## Mechanism confirmation (on buggy 4.1.1)
1. Same frozen-map clustering column but DEFAULT (ASC) order — no ReversedType wrapping — the same
   element selection works fine, proving the fault is specifically the reversed-type wrapping:
```
$ kubectl exec -n repro-17913 cass-411 -- cqlsh -e \
  "CREATE TABLE repro17913_ks.t_asc (k int, c frozen<map<text,int>>, v int, PRIMARY KEY(k,c));"
$ kubectl exec -n repro-17913 cass-411 -- cqlsh -e "SELECT c['testing'] FROM repro17913_ks.t_asc;"

 c['testing']
--------------


(0 rows)
EXIT_RC=0
```
2. Slice/range selection on the DESC table also throws (body says WithSliceSelection is also affected):
```
$ kubectl exec -n repro-17913 cass-411 -- cqlsh -e "SELECT c['a'..'z'] FROM repro17913_ks.t;"
<stdin>:1:InvalidRequest: Error from server: code=2200 [Invalid query] message="Invalid slice selection: c of type frozen<map<text, int>> is not a collection"
command terminated with exit code 2
```

## Verdict
REPRODUCED. Verbatim buggy signature:
`InvalidRequest: Error from server: code=2200 [Invalid query] message="Invalid element selection: c is of type frozen<map<text, int>> is not a collection"`
Control (4.1.2) returns 0 rows on the identical query. Mechanism (ReversedType wrapping on
frozen-collection clustering column) confirmed via the ASC-vs-DESC contrast on the buggy node.

## Tooling findings
None. Official cassandra:4.1.1 and cassandra:4.1.2 images pulled cleanly into kind and the skill's
single-pod template worked without modification.

## Teardown
`kubectl delete ns repro-17913 --wait=false` issued after writing this log.
