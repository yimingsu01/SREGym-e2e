# CASSANDRA-16836 — Materialized views incorrect quoting of UDF

- **Buggy version:** cassandra:3.11.11 (single pod in kind, namespace `repro-16836`)
- **Fix control:** cassandra:3.11.12 (listed fixVersion; 3.11.11+1 <= ceiling 19) — identical workload
- **Component:** Feature/Materialized Views
- **fixVersions:** 3.11.12, 4.1-alpha1, 4.1
- **Topology:** 1 node (HINT confirmed). Body uses RF=3 but error is "Failed to apply mutation **locally**", so RF is irrelevant; used RF=1 to remove a variable.
- **Classifier hint trigger:** "MV with quoted UDF in WHERE + node restart + INSERT -> Unknown function error / WriteFailure" — CONFIRMED, with the additional finding that the failure also fires on the FIRST insert right after MV creation (no restart needed), because the MV's read query is rebuilt from a stored WHERE clause that already lost the quoting.

## Reproducer (extracted from Jira body, adapted to 1 node / RF=1)
```sql
CREATE KEYSPACE repro16836 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
USE repro16836;
CREATE TABLE t (k int PRIMARY KEY, v int);
CREATE FUNCTION "Double" (input int)        -- mixed-case name => REQUIRES quoting
   CALLED ON NULL INPUT RETURNS int LANGUAGE java AS 'return input*2;';
CREATE MATERIALIZED VIEW mv AS SELECT * FROM t
   WHERE k < repro16836."Double"(2) AND k IS NOT NULL AND v IS NOT NULL
   PRIMARY KEY (v, k);
-- then: INSERT INTO t(k,v) VALUES (3,1);   (body says: after a node restart)
```
Note: UDFs are disabled by default in 3.11; pod command appends `enable_user_defined_functions: true`
to cassandra.yaml before the entrypoint. Schema persisted on an emptyDir mounted at /var/lib/cassandra
so a kill-1 in-place restart preserves the on-disk schema (the body's restart trigger).

## ROOT-CAUSE EVIDENCE — stored MV WHERE clause loses the quoting
`DESCRIBE KEYSPACE repro16836` on the buggy node shows the function created WITH quotes but the MV's
persisted WHERE clause re-emitted WITHOUT quotes:
```
CREATE FUNCTION repro16836."Double"(input int) ...           <-- quoted (correct)
CREATE MATERIALIZED VIEW repro16836.mv AS SELECT * FROM repro16836.t
    WHERE k < repro16836.Double(2) AND k IS NOT NULL AND v IS NOT NULL   <-- UNQUOTED (BUG)
```
When the MV read query is (re)parsed, `repro16836.Double(2)` is interpreted as the lowercased
`repro16836.double`, which does not exist -> the local mutation that maintains the MV fails.

## BUGGY SIGNATURE (verbatim, from /var/log/cassandra/system.log on cassandra:3.11.11)
Client (cqlsh) sees:
```
WriteFailure: Error from server: code=1500 [Replica(s) failed to execute write]
message="Operation failed - received 0 responses and 1 failures"
info={'failures': 1, 'received_responses': 0, 'required_responses': 1, 'consistency': 'ONE'}
```
Server-side root exception (the money line + frames, matching the Jira body exactly):
```
org.apache.cassandra.exceptions.InvalidRequestException: Unknown function repro16836.double called
	at org.apache.cassandra.cql3.functions.FunctionCall$Raw.prepare(FunctionCall.java:139)
	at org.apache.cassandra.cql3.SingleColumnRelation.toTerm(SingleColumnRelation.java:122)
	at org.apache.cassandra.cql3.SingleColumnRelation.newSliceRestriction(SingleColumnRelation.java:209)
	at org.apache.cassandra.cql3.Relation.toRestriction(Relation.java:146)
	at org.apache.cassandra.cql3.restrictions.StatementRestrictions.<init>(StatementRestrictions.java:182)
	at org.apache.cassandra.cql3.statements.SelectStatement$RawStatement.prepareRestrictions(SelectStatement.java:1050)
	at org.apache.cassandra.cql3.statements.SelectStatement$RawStatement.prepare(SelectStatement.java:969)
	at org.apache.cassandra.db.view.View.getSelectStatement(View.java:184)
	at org.apache.cassandra.db.view.View.getReadQuery(View.java:199)
	at org.apache.cassandra.db.view.TableViews.updatedViews(TableViews.java:361)
	at org.apache.cassandra.db.view.ViewManager.updatesAffectView(ViewManager.java:83)
	at org.apache.cassandra.db.Keyspace.applyInternal(Keyspace.java:495)
	at org.apache.cassandra.db.Keyspace.apply(Keyspace.java:470)
	at org.apache.cassandra.db.Mutation.apply(...)
	at org.apache.cassandra.service.StorageProxy$8.runMayThrow(StorageProxy.java:1517)
```
This is the same exception/frame chain as the Jira description (the body had test.double; here it is
repro16836.double — the lowercasing of the quoted "Double" is the bug).

## Pre-restart vs post-restart
- Pre-restart INSERT (cached path): also FAILS with the same WriteFailure/Unknown function (restartCount=0).
  => the bug is even broader than the body states; the stored WHERE clause is already wrong at MV creation.
- Post-restart INSERT (body's exact scenario, restartCount=1, schema reloaded from disk): FAILS identically.
  After `kill 1` (in-place restart, emptyDir preserved), restartCount incremented to 1 and the on-disk
  schema reloaded. `DESCRIBE` still shows FUNCTION `"Double"` (quoted) but MV WHERE `repro16836.Double(2)`
  (unquoted). `INSERT INTO repro16836.t(k,v) VALUES (3,1);` returns the same:
  ```
  WriteFailure: Error from server: code=1500 [Replica(s) failed to execute write]
  message="Operation failed - received 0 responses and 1 failures"
  info={'failures': 1, 'received_responses': 0, 'required_responses': 1, 'consistency': 'ONE'}
  ```
  Post-restart server log CAPTURED from the fresh container (restartCount=1, /var/log/cassandra is a new
  file since the emptyDir only covers /var/lib/cassandra; grep run AFTER restart):
  ```
  org.apache.cassandra.exceptions.InvalidRequestException: Unknown function repro16836.double called
  	at org.apache.cassandra.cql3.functions.FunctionCall$Raw.prepare(FunctionCall.java:139) ~[apache-cassandra-3.11.11.jar:3.11.11]
  	at org.apache.cassandra.cql3.SingleColumnRelation.toTerm(SingleColumnRelation.java:122) ~[apache-cassandra-3.11.11.jar:3.11.11]
  	... (same chain through View.getReadQuery -> TableViews.updatedViews -> Keyspace.applyInternal)
  ```
  => Reproduced via BOTH paths: the documented post-restart path AND the first-insert path (same
  prepare-time mechanism firing regardless of restart).

## A/B CONTROL — cassandra:3.11.12 (fix), IDENTICAL workload (pod `cassctl`, same namespace)
Discriminating check is DESCRIBE (quoting), then INSERT:
```
release_version = 3.11.12
DESCRIBE KEYSPACE repro16836:
  CREATE FUNCTION repro16836."Double"(input int) ...                      <-- quoted
  CREATE MATERIALIZED VIEW repro16836.mv AS ...
      WHERE k < repro16836."Double"(2) AND k IS NOT NULL AND v IS NOT NULL  <-- QUOTES PRESERVED (fixed)
INSERT INTO repro16836.t(k,v) VALUES (1,1);  INSERT ... VALUES (3,1);   -> EXIT 0, no error
SELECT * FROM repro16836.t   -> 2 rows (1|1, 3|1)
SELECT * FROM repro16836.mv  -> 2 rows (v|k: 1|1, 1|3)   (MV maintained correctly)
```
The single strongest artifact: same `DESCRIBE` on 3.11.11 emits the WHERE clause UNQUOTED
(`repro16836.Double(2)`) while 3.11.12 emits it QUOTED (`repro16836."Double"(2)`). The control's
function+MV genuinely exist (DESCRIBE confirms), so the INSERT success is meaningful, not a no-op.

## DISPOSITION: reproduced
Verbatim buggy signature: `org.apache.cassandra.exceptions.InvalidRequestException: Unknown function repro16836.double called`
(client surface: `WriteFailure ... code=1500 [Replica(s) failed to execute write]`).

## Tag correction
- topology=1node: CONFIRMED (error is "Failed to apply mutation locally").
- trigger: the hint/body say "node restart" is the trigger; in practice the restart is NOT strictly
  required — the failure also fires on the FIRST insert immediately after MV creation (same prepare-time
  mechanism). Restart still reproduces, matching the body. So the documented restart is sufficient but
  the bug surfaces earlier than documented.
- The body's `replication_factor:3` is unnecessary; RF=1 on a single node reproduces identically.


