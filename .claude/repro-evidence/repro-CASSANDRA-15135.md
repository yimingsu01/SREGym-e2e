# CASSANDRA-15135 Reproduction Evidence

## Bug summary (from Jira primary source)
**Title:** SASI tokenizer options not validated before being added to schema
**Components:** Feature/SASI
**Buggy version tested:** cassandra:4.0.0
**Fix versions:** 3.11.12, 4.0.1, 4.1-alpha1, 4.1
**Control (fixed) image:** cassandra:4.0.1  (4.0.0 patch+1, <= 4.0 ceiling 20)

**Mechanism (from body + PoC commit https://github.com/vincewhite/cassandra/commit/089547946d284ae3feb0d5620067b85b8fd66ebc):**
If you create a SASI index with an illegal argument combination, the index is added to
the schema tables BEFORE the tokenizer/analyzer is instantiated. The analyzer init then
throws an `IllegalArgumentException` (uncaught -> RuntimeException). Because the index row
was already written to `system_schema.indexes`, Cassandra hits the SAME exception when it
loads the schema on boot and FAILS TO START.

The fix (IndexMode.java) calls `analyzer.init(...)` during validation and wraps
`IllegalArgumentException` in a `ConfigurationException` so the bad index is rejected before
the schema write.

**Exact illegal CQL (from the PoC unit test `testIllegalArgumentsException`):**
```
CREATE CUSTOM INDEX illegal_index ON <ks>.<tbl>(v)
USING 'org.apache.cassandra.index.sasi.SASIIndex'
WITH OPTIONS = {'mode':'CONTAINS',
                'analyzer_class':'org.apache.cassandra.index.sasi.analyzer.NonTokenizingAnalyzer',
                'case_sensitive':'false','normalize_uppercase':'true'}
```
`case_sensitive` cannot be combined with `normalize_lowercase`/`normalize_uppercase`.

## Topology
Single Cassandra node (1-replica StatefulSet) in kind, namespace `repro-15135`,
with a persistent volume on `/var/lib/cassandra` (kind local-path / `standard` storage class)
so that "restart" reloads the poisoned schema. `enable_sasi_indexes: true` appended to
cassandra.yaml (SASI is gated off by default in 4.0). Tag hint (1node, H) CONFIRMED correct.

================================================================================
## A. BUGGY IMAGE cassandra:4.0.0
================================================================================

### A0. SASI gate open + a VALID SASI index succeeds (control-for-gate)
Command:
```
kubectl exec -n repro-15135 cass-0 -- cqlsh -e "
CREATE KEYSPACE IF NOT EXISTS repro15135 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE repro15135.t (k int PRIMARY KEY, v text);
CREATE CUSTOM INDEX valid_idx ON repro15135.t(v) USING 'org.apache.cassandra.index.sasi.SASIIndex' WITH OPTIONS = {'mode':'CONTAINS','analyzer_class':'org.apache.cassandra.index.sasi.analyzer.NonTokenizingAnalyzer','case_sensitive':'false'};
SELECT keyspace_name, index_name FROM system_schema.indexes WHERE keyspace_name='repro15135';"
```
Output (valid index created fine -> SASI gate is open):
```
Warnings :
SASI indexes are experimental and are not recommended for production use.

 keyspace_name | index_name
---------------+------------
    repro15135 |  valid_idx
(1 rows)
```

### A1. ILLEGAL index CREATE -> client sees NoHostAvailable (node threw RuntimeException)
Command:
```
kubectl exec -n repro-15135 cass-0 -- cqlsh -e "CREATE CUSTOM INDEX illegal_index ON repro15135.t(v) USING 'org.apache.cassandra.index.sasi.SASIIndex' WITH OPTIONS = {'mode':'CONTAINS','analyzer_class':'org.apache.cassandra.index.sasi.analyzer.NonTokenizingAnalyzer','case_sensitive':'false','normalize_uppercase':'true'}"
```
Output:
```
<stdin>:1:NoHostAvailable: ('Unable to complete the operation against any hosts', {})
command terminated with exit code 2
```

### A2. *** PREMATURE SCHEMA WRITE (the bug mechanism) *** illegal_index IS in system_schema.indexes despite the error
Command:
```
kubectl exec -n repro-15135 cass-0 -- cqlsh -e "SELECT keyspace_name, index_name, options FROM system_schema.indexes WHERE keyspace_name='repro15135'"
```
Output:
```
 keyspace_name | index_name    | options
---------------+---------------+-----------------------------------------------------------------------------------------------------------
    repro15135 | illegal_index | {'analyzer_class': '...NonTokenizingAnalyzer', 'case_sensitive': 'false', 'class_name': '...SASIIndex', 'mode': 'CONTAINS', 'normalize_uppercase': 'true', 'target': 'v'}
    repro15135 |     valid_idx |                                {'analyzer_class': '...NonTokenizingAnalyzer', 'case_sensitive': 'false', 'class_name': '...SASIIndex', 'mode': 'CONTAINS', 'target': 'v'}
(2 rows)
```

### A3. VERBATIM server-side exception at CREATE time (kubectl logs cass-0)
```
ERROR [Native-Transport-Requests-1] 2026-06-12 03:01:15,379 QueryMessage.java:121 - Unexpected error during query
com.google.common.util.concurrent.UncheckedExecutionException: java.lang.RuntimeException: java.lang.reflect.InvocationTargetException
Caused by: java.lang.RuntimeException: java.lang.reflect.InvocationTargetException
Caused by: java.lang.reflect.InvocationTargetException: null
Caused by: java.lang.IllegalArgumentException: case_sensitive option cannot be specified together with either normalize_lowercase or normalize_uppercase
	at org.apache.cassandra.index.sasi.analyzer.NonTokenizingOptions.buildFromMap(NonTokenizingOptions.java:110)
	at org.apache.cassandra.index.sasi.analyzer.NonTokenizingAnalyzer.init(NonTokenizingAnalyzer.java:61)
```

### A4. *** HEADLINE SYMPTOM: node fails to START after restart ***
Restart = `kubectl delete pod cass-0 -n repro-15135` (PVC persists, poisoned schema reloads).
Pod enters CrashLoopBackOff (exitCode 3 each attempt):
```
NAME     READY   STATUS   RESTARTS      AGE
cass-0   0/1     Error    4 (53s ago)   2m3s
state: CrashLoopBackOff / terminated exitCode=3 reason=Error (repeated)
```
Boot-failure log (`kubectl logs cass-0`), after "Initializing system_schema.indexes":
```
INFO  [main] ... ColumnFamilyStore.java:385 - Initializing system_schema.indexes
INFO  [SSTableBatchOpen:1] ... Opening /var/lib/cassandra/data/system_schema/indexes-.../nb-2-big
Exception (java.lang.RuntimeException) encountered during startup: java.lang.reflect.InvocationTargetException
java.lang.RuntimeException: java.lang.reflect.InvocationTargetException
Caused by: java.lang.IllegalArgumentException: case_sensitive option cannot be specified together with either normalize_lowercase or normalize_uppercase
	at org.apache.cassandra.index.sasi.analyzer.NonTokenizingOptions.buildFromMap(NonTokenizingOptions.java:110)
	at org.apache.cassandra.index.sasi.analyzer.NonTokenizingAnalyzer.init(NonTokenizingAnalyzer.java:61)
ERROR [main] 2026-06-12 03:03:33,875 CassandraDaemon.java:909 - Exception encountered during startup
java.lang.RuntimeException: java.lang.reflect.InvocationTargetException
Caused by: java.lang.IllegalArgumentException: case_sensitive option cannot be specified together with either normalize_lowercase or normalize_uppercase
	at org.apache.cassandra.index.sasi.analyzer.NonTokenizingOptions.buildFromMap(NonTokenizingOptions.java:110)
	at org.apache.cassandra.index.sasi.analyzer.NonTokenizingAnalyzer.init(NonTokenizingAnalyzer.java:61)
```

================================================================================
## B. CONTROL — FIXED IMAGE cassandra:4.0.1  (identical YAML + identical illegal CQL)
================================================================================

### B1. IDENTICAL illegal CREATE on fixed 4.0.1 -> clean ConfigurationException (client-visible InvalidRequest)
Command (byte-identical to A1 except image):
```
kubectl exec -n repro-15135 cass-0 -- cqlsh -e "CREATE CUSTOM INDEX illegal_index ON repro15135.t(v) USING 'org.apache.cassandra.index.sasi.SASIIndex' WITH OPTIONS = {'mode':'CONTAINS','analyzer_class':'org.apache.cassandra.index.sasi.analyzer.NonTokenizingAnalyzer','case_sensitive':'false','normalize_uppercase':'true'}"
```
Output:
```
<stdin>:1:ConfigurationException: case_sensitive option cannot be specified together with either normalize_lowercase or normalize_uppercase
command terminated with exit code 2
```

### B2. Index NOT written to schema on the fixed image (the fix prevents the premature write)
```
kubectl exec -n repro-15135 cass-0 -- cqlsh -e "SELECT keyspace_name, index_name FROM system_schema.indexes WHERE keyspace_name='repro15135'"
 keyspace_name | index_name
---------------+------------
(0 rows)
```

### B3. Fixed node restarts CLEAN (no poisoned schema)
`kubectl delete pod cass-0 -n repro-15135` then:
```
NAME     READY   STATUS    RESTARTS   AGE
cass-0   1/1     Running   0          77s
SELECT now() FROM system.local ->  f83dd030-660b-11f1-bfab-174f4f45c353   (answers fine)
```

================================================================================
## CONCLUSION: REPRODUCED
================================================================================
Three-way discriminator (buggy 4.0.0 vs fixed 4.0.1):
| aspect                        | 4.0.0 buggy                          | 4.0.1 fixed                    |
|-------------------------------|--------------------------------------|--------------------------------|
| illegal CREATE response       | NoHostAvailable (uncaught Runtime..) | ConfigurationException (clean) |
| index in system_schema.indexes| YES (premature write)                | NO (0 rows)                    |
| restart after illegal CREATE  | CrashLoopBackOff, FAILS TO BOOT      | clean restart, Ready           |

Both the bug MECHANISM (premature schema write of an unvalidated SASI index) and the
HEADLINE SYMPTOM (node fails to start on restart, CrashLoopBackOff) reproduce on the
exact buggy image cassandra:4.0.0, and are absent on the fixed cassandra:4.0.1.

Most-telling verbatim buggy signature (boot-failure):
  Caused by: java.lang.IllegalArgumentException: case_sensitive option cannot be specified together with either normalize_lowercase or normalize_uppercase
  	at org.apache.cassandra.index.sasi.analyzer.NonTokenizingOptions.buildFromMap(NonTokenizingOptions.java:110)

Classifier tag check: HINT topology=1node, confidence=H, trigger="CREATE CUSTOM INDEX (SASI) with
illegal tokenizer options -> RuntimeException + node fails to start on restart" — ALL CONFIRMED CORRECT
against the Jira body and PoC. (Minor: the failing option is case_sensitive+normalize_uppercase on the
NonTokenizingAnalyzer, not a "tokenizer" per se, but functionally the same illegal-options path.)

