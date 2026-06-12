# CASSANDRA-17848 Reproduction Log

## Issue
- **Key:** CASSANDRA-17848 — "Fix incorrect resource name in LIST PERMISSION output"
- **Component:** CQL/Interpreter
- **fixVersions (Jira):** 3.0.29, 3.11.15, 4.0.8, 4.1.1, 5.0-alpha1, 5.0
- **Candidate buggy version (given):** 4.1.0
- **Classifier hint:** topology=1node, confidence=H,
  trigger="CREATE FUNCTION with quoted name containing [type-like bracket] + LIST EXECUTE OF user -> wrong/failed resource name"

## Reproducer extracted from the Jira body (ground truth)
In cqlsh (auth + authz + UDF enabled):
1. `CREATE FUNCTION test."admin_created_udf[org.apache.cassandra.db.marshal.LongType]"(input int) RETURNS NULL ON NULL INPUT RETURNS int LANGUAGE java AS 'return 42;';`
2. `LIST EXECUTE OF user;`

Expected buggy symptom (per body):
- Resource shown as `<function test.admin_created_udf(long)>` — wrong input type (`long` instead of `int`),
  because LIST-PERMISSION resource-name parsing assumed the `[...]` content was the function's input type.
- If the `[...]` content is not a valid class, LIST PERMISSION fails with
  `ConfigurationException: Unable to find abstract-type class`.

## Topology / setup
- Single Cassandra pod `cass` in namespace `repro-17848`, image `cassandra:4.1.0`
  (digest `sha256:91c8ebb413cb2df929b9e3cd107492b95c10dd0aa8d8a020d1f5ffa27694a88b`).
- cassandra.yaml modified at boot via sed (verified in logs):
  - `authenticator: PasswordAuthenticator`
  - `authorizer: CassandraAuthorizer`
  - `user_defined_functions_enabled: true`
- Auth confirmed: `SELECT now() FROM system.local` answered only with `-u cassandra -p cassandra`.
- Created own keyspace `repro17848` and roles `u1`, `u2` (isolation; did NOT use `test`).

## RESULT: NOT REPRODUCIBLE — fix already present in cassandra:4.1.0

### Decisive evidence A — the image's own CHANGES.txt lists the fix UNDER 4.1.0
```
$ kubectl exec -n repro-17848 cass -- bash -c 'grep -nE "^[45]\.[0-9]" /opt/cassandra/CHANGES.txt | head; grep -n 17848 /opt/cassandra/CHANGES.txt'
1:4.1.0
10:4.1-rc1
35:4.1-beta1
97:4.1-alpha1
...
6: * Fix incorrect resource name in LIST PERMISSION output (CASSANDRA-17848)
```
Line 6 ("...CASSANDRA-17848") sits between the `4.1.0` header (line 1) and the `4.1-rc1` header
(line 10) => the fix shipped in the **4.1.0 GA release**, not 4.1.1. The Jira fixVersions field
(4.1.1) predates/diverges from the actual 4.1.0 GA cut.

### Decisive evidence B — source at git tag cassandra-4.1.0 already has the validation
`src/java/org/apache/cassandra/cql3/functions/FunctionName.java` at tag `cassandra-4.1.0` contains the
fix's new validation (added by the 17848 patch, commit 473656c):
```java
private static final Set<Character> DISALLOWED_CHARACTERS =
    Collections.unmodifiableSet(new HashSet<>(Arrays.asList('/', '[', ']')));

public static boolean isNameValid(String name) {
    for (int i = 0; i < name.length(); i++)
        if (DISALLOWED_CHARACTERS.contains(name.charAt(i))) return false;
    return true;
}
```
`CreateFunctionStatement` calls `isNameValid()` and throws `InvalidRequestException` when it fails.
The fix therefore REJECTS bracket-named UDFs at CREATE time, which shadows (makes unreachable) the
buggy LIST-PERMISSION resource-name parsing path described in the body.

### Decisive evidence C — empirical: CREATE FUNCTION with bracket name is rejected on 4.1.0
Running release version is confirmed 4.1.0:
```
$ kubectl exec ... -e "SELECT release_version FROM system.local;"
 release_version
-----------------
           4.1.0
```

The EXACT Jira reproducer command (valid-class bracket name):
```
$ kubectl exec -n repro-17848 cass -- cqlsh -u cassandra -p cassandra -e \
  'CREATE FUNCTION repro17848."admin_created_udf[org.apache.cassandra.db.marshal.LongType]"(input int) RETURNS NULL ON NULL INPUT RETURNS int LANGUAGE java AS '"'"'return 42;'"'"';'
<stdin>:1:InvalidRequest: Error from server: code=2200 [Invalid query] message="Function name 'admin_created_udf[org.apache.cassandra.db.marshal.LongType]' is invalid"
command terminated with exit code 2
```

Invalid-class bracket name (body's second symptom):
```
<stdin>:1:InvalidRequest: Error from server: code=2200 [Invalid query] message="Function name 'weird[not.a.real.Class]' is invalid"
```

Even the minimal bracket name `f[x]` is rejected:
```
<stdin>:1:InvalidRequest: Error from server: code=2200 [Invalid query] message="Function name 'f[x]' is invalid"
```

### Control within the buggy image — normal-name UDF works and LIST is correct
A non-bracket function creates fine and LIST EXECUTE returns the correct resource (no mis-parse possible
because no bracket function can exist):
```
$ ... CREATE FUNCTION repro17848.normalfn(input int) ... ; DESCRIBE FUNCTIONS;
Keyspace repro17848
-------------------
normalfn(int)

$ ... GRANT EXECUTE ON FUNCTION repro17848.normalfn(int) TO u1; LIST EXECUTE OF u1;
 role | username | resource                            | permission
------+----------+-------------------------------------+------------
   u1 |       u1 | <function repro17848.normalfn(int)> |    EXECUTE
(1 rows)
```
The `LIST EXECUTE OF <role>` machinery itself works; the buggy resource-name path is simply unreachable
because the only way to reach it (a bracket-named UDF) is blocked by the fix's validation.

## A/B fixed control (4.1.1)
Not run: it would be identical. Both `cassandra:4.1.0` and `cassandra:4.1.1` carry the same
`DISALLOWED_CHARACTERS` validation (the fix is in both), so both reject the CREATE FUNCTION. The
in-image CHANGES.txt and the `cassandra-4.1.0` source tag already establish 4.1.0 == fixed.

## Disposition
**not-reproducible.** The candidate's stated buggy version (4.1.0) already contains the CASSANDRA-17848
fix (confirmed three ways: in-image CHANGES.txt under the 4.1.0 header, the cassandra-4.1.0 source tag,
and empirical rejection of bracket-named CREATE FUNCTION). The body's reproducer cannot fire on this
image because the fix's name-validation rejects the bracket-named UDF at prepare time, making the buggy
LIST-PERMISSION resource-name parse path unreachable. The true buggy range is 4.1-rc1 / 4.1-beta / 4.1-alpha
and 4.0.x < 4.0.8 (and 3.x lines), none of which is the given 4.1.0 image.

## Teardown
`kubectl delete ns repro-17848 --wait=false` (executed after writing this log).
