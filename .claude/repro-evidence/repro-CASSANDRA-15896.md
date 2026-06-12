# CASSANDRA-15896 — Reproduction Evidence Log

**Summary:** NullPointerException in `SELECT JSON` statement when a UUID field contains an empty string.
**Buggy version:** cassandra:3.11.7  **Control (fixed) version:** cassandra:3.11.8 (fix landed in 3.11.8)
**fixVersions (Jira):** 3.0.22, 3.11.8, 4.0-beta2, 4.0
**Components:** CQL/Interpreter, CQL/Semantics
**Topology:** single node (matches classifier hint `1node`). Confidence hint H — confirmed.
**Disposition:** REPRODUCED.

## Reproducer extracted from Jira body
The Jira description ships a Java-driver JUnit test, but the mechanism is pure CQL:
1. Table with a `uuid` column.
2. `INSERT ... JSON` with **empty string `""`** (NOT null) for the uuid field — Cassandra accepts it
   and stores a zero-length value.
3. `SELECT JSON <uuid_col>` of that row -> server NPE at
   `org.apache.cassandra.db.marshal.AbstractType.toJSONString`.
The driver's prepared-statement path and the literal `INSERT ... JSON '{...}'` path hit the same
parser, so the Java driver was dropped; reproduced entirely via `cqlsh`. (tag_correction: none — the
"INSERT JSON empty-string uuid -> SELECT JSON -> NPE" hint is exactly the body's mechanism.)

## Environment
- Existing kind cluster, context `kind-kind`.
- Namespace created: `repro-15896`. Keyspace: `repro15896`.
- Two single-node pods in that ns: `cass` (cassandra:3.11.7, buggy) and `cass-ctl` (cassandra:3.11.8, control).

## Workload (identical on both pods, via stdin heredoc)
```
kubectl exec -i -n repro-15896 <pod> -- cqlsh <<'EOF'
CREATE KEYSPACE IF NOT EXISTS repro15896 WITH replication={'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE IF NOT EXISTS repro15896.t (id uuid PRIMARY KEY, another_id uuid, subject text);
INSERT INTO repro15896.t JSON '{"id":"11111111-1111-1111-1111-111111111111","another_id":"","subject":"dante"}';
SELECT JSON id, another_id FROM repro15896.t;
EOF
```

## BUGGY 3.11.7 (pod `cass`) — RAW OUTPUT
Client (cqlsh): the SELECT JSON (line 5) fails:
```
<stdin>:5:ServerError: java.lang.NullPointerException
command terminated with exit code 2
```
Plain `SELECT id, another_id, subject` confirms the empty-string UUID was stored (blank value):
```
 id                                   | another_id | subject
--------------------------------------+------------+---------
 11111111-1111-1111-1111-111111111111 |            |   dante
(1 rows)
```

Server log (`kubectl logs -n repro-15896 cass`) — the telling frames (VERBATIM):
```
ERROR [Native-Transport-Requests-1] 2026-06-12 02:36:05,274 ErrorMessage.java:384 - Unexpected exception during request
java.lang.NullPointerException: null
	at org.apache.cassandra.db.marshal.AbstractType.toJSONString(AbstractType.java:156) ~[apache-cassandra-3.11.7.jar:3.11.7]
	at org.apache.cassandra.cql3.selection.Selection.rowToJson(Selection.java:343) ~[apache-cassandra-3.11.7.jar:3.11.7]
	at org.apache.cassandra.cql3.selection.Selection$ResultSetBuilder.getOutputRow(Selection.java:494) ~[apache-cassandra-3.11.7.jar:3.11.7]
	at org.apache.cassandra.cql3.selection.Selection$ResultSetBuilder.build(Selection.java:477) ~[apache-cassandra-3.11.7.jar:3.11.7]
	at org.apache.cassandra.cql3.statements.SelectStatement.process(SelectStatement.java:794) ~[apache-cassandra-3.11.7.jar:3.11.7]
	at org.apache.cassandra.cql3.statements.SelectStatement.processResults(SelectStatement.java:438) ~[apache-cassandra-3.11.7.jar:3.11.7]
	at org.apache.cassandra.cql3.statements.SelectStatement.execute(SelectStatement.java:416) ~[apache-cassandra-3.11.7.jar:3.11.7]
	... QueryProcessor.process -> QueryMessage.execute -> Message$Dispatcher ...
	at java.lang.Thread.run(Thread.java:748) ~[na:1.8.0_262]
```
The frame `AbstractType.toJSONString(AbstractType.java:156)` + `Selection.rowToJson(Selection.java:343)`
matches the Jira ground-truth stack exactly (ticket showed 3.11.6; here 3.11.7, same line 156).

## CONTROL 3.11.8 (pod `cass-ctl`) — RAW OUTPUT (identical workload)
Client (cqlsh) — INSERT still accepted, SELECT JSON returns clean JSON, exit 0:
```
 [json]
------------------------------------------------------------------
 {"id": "11111111-1111-1111-1111-111111111111", "another_id": ""}
(1 rows)
EXIT_CODE=0
```
Server log NPE count on `cass-ctl`: **0**.
The fix chose the "accept the empty string and serialize it cleanly" path (not reject-at-INSERT);
`another_id` round-trips as `""` in the JSON output with no exception.

## Conclusion
- BUGGY 3.11.7: `SELECT JSON` over a UUID column holding an empty string -> server-side
  `java.lang.NullPointerException` at `AbstractType.toJSONString(AbstractType.java:156)` (client sees
  `ServerError: java.lang.NullPointerException`). Reproduced with verbatim signature.
- CONTROL 3.11.8: identical workload, no NPE, clean JSON. A/B confirms the fix.
- Disposition: **reproduced**.

## Teardown
`kubectl delete ns repro-15896 --wait=false` (executed after writing this log).
