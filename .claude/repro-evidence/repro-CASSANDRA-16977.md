# CASSANDRA-16977 — ArrayIndexOutOfBoundsException in FunctionResource#fromName

## Disposition: REPRODUCED

## Primary source (Jira)
- summary: "ArrayIndexOutOfBoundsException in FunctionResource#fromName"
- description: `FunctionResource` can't handle functions with 0 args; throws
  `java.lang.ArrayIndexOutOfBoundsException: 1` at `FunctionResource.fromName(FunctionResource.java:178)`.
- fixVersions: 3.0.26, 3.11.12, 4.0.2, 4.1-alpha1, 4.1
- components: Feature/Authorization

## Classifier hint vs. reality (tag_correction)
- HINT: topology=1node, confidence=M, trigger="Reference/grant permission on a user function with zero
  arguments -> FunctionResource.fromName throws ArrayIndexOutOfBoundsException".
- REALITY: hint is accurate. 1-node is correct. The trigger is precise but needs one nuance: the GRANT
  itself succeeds (the resource string `functions/ks/fn[]` is *written*); the AIOOBE fires when that stored
  resource is *parsed back* via `Resources.fromName -> FunctionResource.fromName`, which happens on
  `LIST ... PERMISSIONS` (read path: CassandraAuthorizer.listPermissionsForRole). Requires
  authentication + CassandraAuthorizer enabled (Feature/Authorization) and UDFs enabled.
- Jira body cites line 178; the buggy 4.0.1 build throws at line 190 — same method, same code, line-number
  drift across branches. Mechanism identical.

## Root cause (source, cassandra-4.0.1 FunctionResource.fromName)
```java
String[] nameAndArgs = StringUtils.split(parts[2], "[|]");
return function(parts[1], nameAndArgs[0], argsListFromString(nameAndArgs[1]));
```
For a zero-arg function the resource name is `functions/<ks>/<fn>[]`. `StringUtils.split("<fn>[]", "[|]")`
drops the empty trailing token, yielding `["<fn>"]` (length 1), so `nameAndArgs[1]` -> AIOOBE: index 1.

## Environment
- kind cluster context kind-kind, 4 nodes. Namespace: repro-16977 (created by me).
- Buggy image: cassandra:4.0.1 (pod `cass`, pinned to kind-worker).
- Fixed control image: cassandra:4.0.2 (pod `cass-fixed`, pinned to kind-worker). 4.0.2 = buggy(4.0.1)+1 <= 4.0 ceiling(20).
- Images pulled from mirror.gcr.io/library/cassandra:{4.0.1,4.0.2}, retagged docker.io/library/cassandra,
  imported into kind-worker containerd via `ctr -n k8s.io images import` (kind load to control-plane failed
  on a multi-arch digest conflict — see tooling_findings).
- Config applied at pod start by editing /etc/cassandra/cassandra.yaml before docker-entrypoint:
    authenticator: PasswordAuthenticator
    authorizer: CassandraAuthorizer
    enable_user_defined_functions: true
  (confirmed echoed in pod startup log.)

## Reproducer workload (run as superuser cassandra/cassandra)
```
CREATE KEYSPACE IF NOT EXISTS repro16977 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE OR REPLACE FUNCTION repro16977.zeroarg() CALLED ON NULL INPUT RETURNS bigint LANGUAGE java AS 'return System.currentTimeMillis();';
CREATE ROLE IF NOT EXISTS bob WITH PASSWORD = 'bob' AND LOGIN = true;
GRANT EXECUTE ON FUNCTION repro16977.zeroarg() TO bob;   -- succeeds, writes resource functions/repro16977/zeroarg[]
LIST ALL PERMISSIONS OF bob;                              -- triggers fromName parse -> AIOOBE
```
Confirmed the function is genuinely zero-arg:
```
 keyspace_name | function_name | argument_types
---------------+---------------+----------------
    repro16977 |       zeroarg |               []
```

## BUGGY (cassandra:4.0.1) — verbatim output

Client side (cqlsh), on `LIST ALL PERMISSIONS OF bob;`:
```
<stdin>:1:NoHostAvailable: ('Unable to complete the operation against any hosts', {})
command terminated with exit code 2
```
(NoHostAvailable is the driver's surfacing of the coordinator returning a server error for the request.)

Server side (/var/log/cassandra/system.log) — VERBATIM:
```
ERROR [Native-Transport-Requests-1] 2026-06-12 04:46:47,992 ErrorMessage.java:457 - Unexpected exception during request
java.lang.ArrayIndexOutOfBoundsException: Index 1 out of bounds for length 1
	at org.apache.cassandra.auth.FunctionResource.fromName(FunctionResource.java:190)
	at org.apache.cassandra.auth.Resources.fromName(Resources.java:60)
	at org.apache.cassandra.auth.CassandraAuthorizer.listPermissionsForRole(CassandraAuthorizer.java:282)
	at org.apache.cassandra.auth.CassandraAuthorizer.list(CassandraAuthorizer.java:262)
	at org.apache.cassandra.cql3.statements.ListPermissionsStatement.list(ListPermissionsStatement.java:112)
	at org.apache.cassandra.cql3.statements.ListPermissionsStatement.execute(ListPermissionsStatement.java:100)
	at org.apache.cassandra.cql3.statements.AuthorizationStatement.execute(AuthorizationStatement.java:43)
	at org.apache.cassandra.cql3.QueryProcessor.processStatement(QueryProcessor.java:222)
	at org.apache.cassandra.cql3.QueryProcessor.process(QueryProcessor.java:259)
	at org.apache.cassandra.cql3.QueryProcessor.process(QueryProcessor.java:246)
	at org.apache.cassandra.transport.messages.QueryMessage.execute(QueryMessage.java:108)
	at org.apache.cassandra.transport.Message$Request.execute(Message.java:242)
	at org.apache.cassandra.transport.Dispatcher.processRequest(Dispatcher.java:86)
	...
```
This matches the Jira description exactly: ArrayIndexOutOfBoundsException at FunctionResource.fromName,
driven by parsing a zero-arg function authorization resource.

## CONTROL (cassandra:4.0.2, fixed) — identical workload
`LIST ALL PERMISSIONS OF bob;` succeeds, returns the row, NO exception, NO error in log:
```
 role | username | resource                        | permission
------+----------+---------------------------------+------------
  bob |      bob | <function repro16977.zeroarg()> |    EXECUTE

(1 rows)
```
list-exit=0. The fixed build parses `functions/repro16977/zeroarg[]` correctly and renders the resource as
`<function repro16977.zeroarg()>`.

## Conclusion
A/B confirmed. Buggy 4.0.1 throws AIOOBE in FunctionResource.fromName on LIST PERMISSIONS for a zero-arg UDF;
fixed 4.0.2 returns the permission cleanly under the identical workload. Verbatim server-side signature
captured. REPRODUCED.

## Teardown
`kubectl delete ns repro-16977 --wait=false` (only namespace created).
