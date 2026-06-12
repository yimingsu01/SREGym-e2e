# CASSANDRA-19889 — Reproduction Evidence Log

**Summary:** Indexing a frozen collection that is the clustering key and reversed is rejected
**Buggy version:** cassandra:5.0.1 (single node pod)
**Fixed control:** cassandra:5.0.2 (fixVersions include 5.0.2; ceiling for 5.0 line is 8, so 5.0.2 is a valid A/B control image)
**Components:** CQL/Interpreter
**fixVersions:** 4.0.15, 4.1.8, 5.0.2, 6.0-alpha1, 6.0
**Topology:** 1 node (matches classifier hint topology=1node, confidence=H)
**Disposition:** REPRODUCED

## Primary source (Jira body) — exact reproducer extracted

```
CREATE TABLE tbl (
  pk int,
  ck frozen<list<int>>,
  value int,
  PRIMARY KEY(pk, ck)
)
WITH CLUSTERING ORDER BY (ck DESC)

CREATE INDEX ON tbl(FULL(ck));   -- fails
```

Expected failure (server-side stack, per Jira):
```
Caused by: org.apache.cassandra.exceptions.InvalidRequestException: full() indexes can only be created on frozen collections
  at org.apache.cassandra.cql3.statements.schema.AlterSchemaStatement.ire(AlterSchemaStatement.java:222)
  at org.apache.cassandra.cql3.statements.schema.CreateIndexStatement.validateIndexTarget(CreateIndexStatement.java:250)
  at org.apache.cassandra.cql3.statements.schema.CreateIndexStatement.lambda$apply$1(CreateIndexStatement.java:177)
```

Root cause (from body): the clustering key `ck` is wrapped in `ReverseType` because of `CLUSTERING ORDER BY (ck DESC)`.
The FULL()-index validation checks `isFrozenCollection()` without unwrapping `ReverseType`, so a valid index on a
frozen collection is wrongly rejected. cqlsh surfaces the client-side form of the same `InvalidRequestException`
(no Java frames over the wire) — the load-bearing message string is identical.

## Environment

```
$ kubectl config current-context
kind-kind                       (4 nodes: control-plane + 3 workers)

Namespace created: repro-19889  (isolation)
Keyspace:          repro19889_ks (unique)

$ kubectl get pods -n repro-19889 -o wide
NAME         READY   STATUS    RESTARTS   AGE    IP            NODE
cass         1/1     Running   0          ...    10.244.2.27   kind-worker2   (image cassandra:5.0.1 = BUGGY)
cass-fixed   1/1     Running   0          ...    10.244.3.30   kind-worker    (image cassandra:5.0.2 = FIXED control)

$ kubectl exec -n repro-19889 cass       -- cqlsh -e "SHOW VERSION"
[cqlsh 6.2.0 | Cassandra 5.0.1 | CQL spec 3.4.7 | Native protocol v5]
$ kubectl exec -n repro-19889 cass-fixed -- cqlsh -e "SHOW VERSION"
[cqlsh 6.2.0 | Cassandra 5.0.2 | CQL spec 3.4.7 | Native protocol v5]
```

## BUGGY 5.0.1 — reproducer (raw output)

```
$ kubectl exec -n repro-19889 cass -- cqlsh -e \
  "CREATE KEYSPACE repro19889_ks WITH replication = {'class':'SimpleStrategy','replication_factor':1};"
   (ok, no output)

$ kubectl exec -n repro-19889 cass -- cqlsh -e \
  "CREATE TABLE repro19889_ks.tbl (pk int, ck frozen<list<int>>, value int, PRIMARY KEY(pk, ck)) WITH CLUSTERING ORDER BY (ck DESC);"
   (ok, no output)

$ kubectl exec -n repro-19889 cass -- cqlsh -e "CREATE INDEX ON repro19889_ks.tbl(FULL(ck));"
<stdin>:1:InvalidRequest: Error from server: code=2200 [Invalid query] message="full() indexes can only be created on frozen collections"
command terminated with exit code 2
RC=2
```

### <<< VERBATIM BUGGY SIGNATURE >>>
```
<stdin>:1:InvalidRequest: Error from server: code=2200 [Invalid query] message="full() indexes can only be created on frozen collections"
```
A `CREATE INDEX ... FULL(ck)` on a frozen<list<int>> clustering key is wrongly rejected — the index is valid;
the only difference from a working table is the `CLUSTERING ORDER BY (ck DESC)` (ReverseType).

## STRENGTHENER on buggy 5.0.1 — same table WITHOUT `CLUSTERING ORDER BY (ck DESC)` (isolates ReverseType)

```
$ kubectl exec -n repro-19889 cass -- cqlsh -e \
  "CREATE TABLE repro19889_ks.tbl2 (pk int, ck frozen<list<int>>, value int, PRIMARY KEY(pk, ck));"
   (ok)

$ kubectl exec -n repro-19889 cass -- cqlsh -e "CREATE INDEX ON repro19889_ks.tbl2(FULL(ck));"
RC=0    (SUCCEEDS)

$ kubectl exec -n repro-19889 cass -- cqlsh -e \
  "SELECT index_name, kind, options FROM system_schema.indexes WHERE keyspace_name='repro19889_ks';"
 index_name  | kind       | options
-------------+------------+------------------------
 tbl2_ck_idx | COMPOSITES | {'target': 'full(ck)'}
(1 rows)
```
=> On the SAME buggy node, the identical FULL(ck) index on the identical frozen<list<int>> column SUCCEEDS
   when the column is NOT reversed. This pins the trigger to ReverseType (the DESC clustering order), exactly
   as the Jira body states.

## A/B CONTROL — fixed 5.0.2, IDENTICAL workload (table WITH `CLUSTERING ORDER BY (ck DESC)`)

```
$ kubectl exec -n repro-19889 cass-fixed -- cqlsh -e \
  "CREATE KEYSPACE repro19889_ks WITH replication = {'class':'SimpleStrategy','replication_factor':1};"
   (ok)

$ kubectl exec -n repro-19889 cass-fixed -- cqlsh -e \
  "CREATE TABLE repro19889_ks.tbl (pk int, ck frozen<list<int>>, value int, PRIMARY KEY(pk, ck)) WITH CLUSTERING ORDER BY (ck DESC);"
   (ok)

$ kubectl exec -n repro-19889 cass-fixed -- cqlsh -e "CREATE INDEX ON repro19889_ks.tbl(FULL(ck));"
RC=0    (SUCCEEDS — bug fixed)

$ kubectl exec -n repro-19889 cass-fixed -- cqlsh -e \
  "SELECT index_name, kind, options FROM system_schema.indexes WHERE keyspace_name='repro19889_ks';"
 index_name | kind       | options
------------+------------+------------------------
 tbl_ck_idx | COMPOSITES | {'target': 'full(ck)'}
(1 rows)
```
=> Fixed 5.0.2 ACCEPTS the exact same DDL that 5.0.1 rejects. Index `tbl_ck_idx` is created successfully.

## Conclusion

- Buggy 5.0.1: `CREATE INDEX ON tbl(FULL(ck))` on a reversed (DESC) frozen<list<int>> clustering key is
  rejected with `full() indexes can only be created on frozen collections` (InvalidRequest code=2200).
- Fixed 5.0.2: identical statement succeeds.
- Same buggy node accepts FULL(ck) when the column is not reversed -> trigger is ReverseType, matching the
  Jira root-cause ("We have a ReverseType column! We must unwrap the type before this check").
- Classifier tags (topology=1node, confidence=H, trigger) match the body. No tag correction needed.

DISPOSITION: reproduced.
