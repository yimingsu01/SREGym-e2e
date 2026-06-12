# CASSANDRA-19891 Reproduction Evidence Log

## Bug
**Summary:** SAI fails queries when multiple columns exist and a non-indexed column is a CompositeType with a MapType inside.
**Component:** Feature/2i Index
**Buggy version:** 5.0.4   **Fix version:** 5.0.5 (also 6.0-alpha1, 6.0)
**Classifier hint:** topology=1node, confidence=H, trigger=SAI indexes incl. a CompositeType-with-MapType column + multi-column WHERE + ALLOW FILTERING -> query plan fails (single-column case passes).
**Tag correction:** NONE. Hint matched the Jira body exactly. 1-node, pure CQL reproducer. The simplified SAI-only test from the body was used. The key non-indexed column is `r5` ('CompositeType(CompositeType(...),CompositeType(FloatType),MapType(ByteType,TimeType))'); its presence in the multi-column WHERE is what triggers the failure.

## Topology
Single Cassandra pod per version in namespace `repro-19891` on kind (context kind-kind).
- pod `cass`        = cassandra:5.0.4  (buggy)
- pod `cass-fixed`  = cassandra:5.0.5  (fixed A/B control; 5.0.5 <= ceiling 8 for 5.0 line)
Keyspace: `keyspace_test_19891` (SimpleStrategy RF=1). Table empty (bug fires at query-plan time, before touching data).

## Reproducer (extracted from Jira body, "simplified" SAI-only test)
Schema (/tmp/repro_schema.cql) creates the table with all 11 columns and 5 SAI indexes:
`FULL(ck1)`, `FULL(pk1)`, `FULL(r4)`, `r2`, `r3` — all USING 'SAI'.
Two queries (separate files, run separately):
- /tmp/repro_single.cql : single-column `WHERE "r3" = 0x... ALLOW FILTERING`  (control — passes)
- /tmp/repro_multi.cql  : multi-column `WHERE "r5" = 0x... AND "r3" = 0x... AND "r2" = 0x... AND "pk2" = ((-1.2651989E-23)) ALLOW FILTERING`  (trigger — fails)

## Commands + raw outputs

### Schema applied on 5.0.4 — SUCCESS (all 5 SAI indexes created)
```
$ kubectl exec -n repro-19891 cass -- cqlsh -f /tmp/repro_schema.cql ; echo EXIT=$?
EXIT=0

$ kubectl exec -n repro-19891 cass -- cqlsh -e "SELECT index_name, kind, options FROM system_schema.indexes WHERE keyspace_name='keyspace_test_19891' AND table_name='tbl'"
 index_name  | kind   | options
-------------+--------+----------------------------------------------
 tbl_ck1_idx | CUSTOM | {'class_name': 'SAI', 'target': 'full(ck1)'}
 tbl_pk1_idx | CUSTOM | {'class_name': 'SAI', 'target': 'full(pk1)'}
  tbl_r2_idx | CUSTOM |        {'class_name': 'SAI', 'target': 'r2'}
  tbl_r3_idx | CUSTOM |        {'class_name': 'SAI', 'target': 'r3'}
  tbl_r4_idx | CUSTOM |  {'class_name': 'SAI', 'target': 'full(r4)'}
(5 rows)
```

### TEST 1 (CONTROL, within-version): single-column query on r3, 5.0.4 — SUCCEEDS
```
$ kubectl exec -n repro-19891 cass -- cqlsh -f /tmp/repro_single.cql ; echo EXIT=$?
 pk1 | pk2 | ck1 | ck2 | r1 | r2 | r3 | r4 | r5 | r6
-----+-----+-----+-----+----+----+----+----+----+----
(0 rows)
EXIT=0
```

### TEST 2 (TRIGGER): multi-column query (r5 + r3 + r2 + pk2), 5.0.4 — FAILS
Client (cqlsh) side:
```
$ kubectl exec -n repro-19891 cass -- cqlsh -f /tmp/repro_multi.cql ; echo EXIT=$?
/tmp/repro_multi.cql:8:ReadFailure: Error from server: code=1300 [Replica(s) failed to execute read] message="Operation failed - received 0 responses and 1 failures: UNKNOWN from /10.244.3.48:7000" info={'consistency': 'ONE', 'required_responses': 1, 'received_responses': 0, 'failures': 1, 'error_code_map': {'10.244.3.48': '0x0000'}}
command terminated with exit code 2
EXIT=2
```

Server-side full stack trace (kubectl logs -n repro-19891 cass) — THE BUG:
```
ERROR [ReadStage-1] 2026-06-12 03:07:10,629 JVMStabilityInspector.java:70 - Exception in thread Thread[ReadStage-1,10,SharedPool]
java.lang.RuntimeException: java.lang.IllegalArgumentException: Unsupported collection type: map
	at org.apache.cassandra.service.StorageProxy$DroppableRunnable.run(StorageProxy.java:2612)
	at org.apache.cassandra.concurrent.ExecutionFailure$2.run(ExecutionFailure.java:163)
	at org.apache.cassandra.concurrent.SEPWorker.run(SEPWorker.java:143)
Caused by: java.lang.IllegalArgumentException: Unsupported collection type: map
	at org.apache.cassandra.index.sai.utils.IndexTermType.collectionCellValueType(IndexTermType.java:789)
	at org.apache.cassandra.index.sai.utils.IndexTermType.calculateIndexType(IndexTermType.java:726)
	at org.apache.cassandra.index.sai.utils.IndexTermType.calculateCapabilities(IndexTermType.java:672)
	at org.apache.cassandra.index.sai.utils.IndexTermType.<init>(IndexTermType.java:142)
	at org.apache.cassandra.index.sai.utils.IndexTermType.<init>(IndexTermType.java:155)
	at org.apache.cassandra.index.sai.utils.IndexTermType.create(IndexTermType.java:135)
	at org.apache.cassandra.index.sai.plan.Operation.buildUnindexedExpression(Operation.java:163)
	at org.apache.cassandra.index.sai.plan.Operation.buildIndexExpressions(Operation.java:139)
	at org.apache.cassandra.index.sai.plan.Operation$AndNode.analyze(Operation.java:446)
	at org.apache.cassandra.index.sai.plan.Operation$Node.doTreeAnalysis(Operation.java:409)
	at org.apache.cassandra.index.sai.plan.Operation$Node.analyzeTree(Operation.java:394)
	at org.apache.cassandra.index.sai.plan.Operation.buildIterator(Operation.java:328)
	at org.apache.cassandra.index.sai.plan.StorageAttachedIndexSearcher$ResultRetriever.<init>(StorageAttachedIndexSearcher.java:156)
	at org.apache.cassandra.index.sai.plan.StorageAttachedIndexSearcher.search(StorageAttachedIndexSearcher.java:116)
	at org.apache.cassandra.db.ReadCommand.executeLocally(ReadCommand.java:452)
	at org.apache.cassandra.service.StorageProxy$LocalReadRunnable.runMayThrow(StorageProxy.java:2209)
	at org.apache.cassandra.service.StorageProxy$DroppableRunnable.run(StorageProxy.java:2608)
```

Mechanism confirmed: SAI query-plan construction calls `Operation.buildUnindexedExpression` for the
non-indexed column `r5` (CompositeType-with-MapType). `IndexTermType.create` ->
`collectionCellValueType` cannot handle the embedded `map` and throws
`IllegalArgumentException: Unsupported collection type: map`, surfacing to the client as a
ReadFailure (code=1300, "1 failures: UNKNOWN"). Matches the Jira body exactly (Feature/2i Index).

### A/B CONTROL: identical multi-column query on 5.0.5 (FIXED) — SUCCEEDS
```
$ kubectl exec -n repro-19891 cass-fixed -- cqlsh -f /tmp/repro_schema.cql ; echo SCHEMA_EXIT=$?
SCHEMA_EXIT=0
   (5 SAI indexes created: tbl_ck1_idx, tbl_pk1_idx, tbl_r2_idx, tbl_r3_idx, tbl_r4_idx)

$ kubectl exec -n repro-19891 cass-fixed -- cqlsh -f /tmp/repro_multi.cql ; echo EXIT=$?
 pk1 | pk2 | ck1 | ck2 | r1 | r2 | r3 | r4 | r5 | r6
-----+-----+-----+-----+----+----+----+----+----+----
(0 rows)
EXIT=0

$ kubectl logs -n repro-19891 cass-fixed | grep -c "Unsupported collection type"
0
```

### Version confirmation
```
5.0.4  (pod cass, buggy)
5.0.5  (pod cass-fixed, fixed)
```

## Evidence triad (the discriminator that proves CASSANDRA-19891)
1. Single-column query on 5.0.4 -> SUCCEEDS (0 rows).
2. Multi-column query (adds non-indexed r5 CompositeType-with-MapType) on 5.0.4 -> FAILS with
   `IllegalArgumentException: Unsupported collection type: map` at
   `IndexTermType.collectionCellValueType` via `Operation.buildUnindexedExpression`.
3. IDENTICAL multi-column query on 5.0.5 -> SUCCEEDS (0 rows); zero occurrences of the exception in log.

## Disposition: REPRODUCED
Verbatim buggy signature:
`Caused by: java.lang.IllegalArgumentException: Unsupported collection type: map` /
`at org.apache.cassandra.index.sai.utils.IndexTermType.collectionCellValueType(IndexTermType.java:789)`
... `at org.apache.cassandra.index.sai.plan.Operation.buildUnindexedExpression(Operation.java:163)`

## Teardown
`kubectl delete ns repro-19891 --wait=false` (only namespace created by this session).
