# CASSANDRA-12949 — Reproduction Evidence

**Summary (Jira):** CFS.setCompressionParameters() method can affect schema globally
**Buggy version:** cassandra:3.11.10   **Fixed control:** cassandra:3.11.11 (fixVersions: 3.0.25, 3.11.11, 4.0-rc1, 4.0)
**Component:** Legacy/Distributed Metadata
**Namespace:** repro-12949   **Keyspace:** repro12949_ks   **Topology:** single pod (see tag_correction)
**Disposition:** REPRODUCED

## Bug mechanism (from Jira description, the ground truth)
> CFS.setCompressionParameters() ... is intended as a way to change compression locally on just one node,
> for experimental purposes. ... Its modification by CFS.setCompressionParameters() means that any subsequent
> ALTER that affects that table will pick up the change made by CFS.setCompressionParameters() and disseminate
> it to the rest of the cluster.

setCompactionParameters() never mutates CFMetaData in-place; setCompressionParameters() DOES mutate it in-place,
so a later (otherwise unrelated) ALTER serializes that mutated in-memory CFMetaData into the schema migration.

## Reproducer (exact 3 steps)
(a) CREATE TABLE ... WITH compression = {'class':'LZ4Compressor','chunk_length_in_kb':64}  -> persisted=64
(b) JMX set the writable attribute `CompressionParameters` (this IS the setCompressionParameters() setter) to
    chunk_length_in_kb=128 on the node. Local-only by design -> persisted schema must stay 64.
(c) Run an UNRELATED `ALTER TABLE ... WITH comment='...'` (touches only the comment, NOT compression).
    BUG: persisted compression flips to 128.

### JMX access note (make-or-break)
- The cassandra:3.11.x image is JRE-only (no javac), and the host has no Java at all. BUT the JRE ships
  `jjs` (Nashorn). I drove JMX from Nashorn JavaScript -> no compilation needed.
- `setCompressionParameters(Map)` is NOT exposed as a JMX *operation* in 3.11 (invoke() failed with
  `NoSuchMethodException: setCompressionParameters(java.util.Map)`); it is exposed as a *writable attribute*
  `CompressionParameters : java.util.Map writable=true`. So I used `setAttribute`, not `invoke`.
- JMX endpoint: service:jmx:rmi:///jndi/rmi://127.0.0.1:7199/jmxrmi (local, unauthenticated — same port nodetool uses).
- MBean: org.apache.cassandra.db:type=ColumnFamilies,keyspace=repro12949_ks,columnfamily=t1

## ===================== BUGGY: cassandra:3.11.10 =====================

release_version = 3.11.10

### (a) Baseline — `SELECT compression FROM system_schema.tables WHERE keyspace_name='repro12949_ks' AND table_name='t1';`
```
 compression
-----------------------------------------------------------------------------------------
 {'chunk_length_in_kb': '64', 'class': 'org.apache.cassandra.io.compress.LZ4Compressor'}

(1 rows)
```

### (b) JMX setAttribute CompressionParameters -> chunk_length_in_kb=128
`kubectl exec -n repro-12949 cass -- /opt/java/openjdk/bin/jjs /tmp/jmxset.js -- repro12949_ks t1 128`
```
TARGET: org.apache.cassandra.db:type=ColumnFamilies,keyspace=repro12949_ks,columnfamily=t1
CURRENT CompressionParameters (raw): {chunk_length_in_kb=64, class=org.apache.cassandra.io.compress.LZ4Compressor}
NEW map to set: {chunk_length_in_kb=128, class=org.apache.cassandra.io.compress.LZ4Compressor}
setAttribute(CompressionParameters) OK
AFTER CompressionParameters (raw): {chunk_length_in_kb=128, class=org.apache.cassandra.io.compress.LZ4Compressor}
```
Schema still 64 immediately after the JMX call (JMX correctly local-only at this point):
```
 compression
-----------------------------------------------------------------------------------------
 {'chunk_length_in_kb': '64', 'class': 'org.apache.cassandra.io.compress.LZ4Compressor'}
```

### (c) UNRELATED ALTER then re-SELECT  <<< THE BUG >>>
`ALTER TABLE repro12949_ks.t1 WITH comment='trigger-12949';`  (never mentions compression)
```
 compression
------------------------------------------------------------------------------------------
 {'chunk_length_in_kb': '128', 'class': 'org.apache.cassandra.io.compress.LZ4Compressor'}

(1 rows)
```
DESCRIBE TABLE confirms both the new comment AND the leaked compression now persisted:
```
    AND comment = 'trigger-12949'
    AND compression = {'chunk_length_in_kb': '128', 'class': 'org.apache.cassandra.io.compress.LZ4Compressor'}
```
==> An ALTER that changed ONLY the comment silently persisted the JMX-set compression (64 -> 128).
    On a cluster this persisted/announced schema mutation disseminates to all nodes (the defect).

## ===================== FIXED CONTROL: cassandra:3.11.11 =====================

release_version = 3.11.11  | identical workload (same /tmp/jmxset.js, same CQL)

(a) baseline:
```
 {'chunk_length_in_kb': '64', 'class': 'org.apache.cassandra.io.compress.LZ4Compressor'}
```
(b) JMX setAttribute -> in-memory 128 ("AFTER ... =128"), persisted schema still 64:
```
 {'chunk_length_in_kb': '64', 'class': 'org.apache.cassandra.io.compress.LZ4Compressor'}
```
(c) UNRELATED ALTER `WITH comment='trigger-12949'` then re-SELECT -> STILL 64 (bug fixed):
```
 {'chunk_length_in_kb': '64', 'class': 'org.apache.cassandra.io.compress.LZ4Compressor'}
```

## A/B verdict
| step | buggy 3.11.10 | fixed 3.11.11 |
|------|---------------|---------------|
| after JMX set (persisted)        | 64  | 64  |
| after unrelated ALTER (persisted)| **128 (LEAK)** | 64 (correct) |

Deterministic, reproduced on first attempt. Verbatim buggy signature is the post-ALTER row showing
`chunk_length_in_kb': '128'` after an ALTER that only set the comment.

## tag_correction
Classifier hint said topology=ring. Single node is sufficient and was used: the defect is that the
in-place-mutated CFMetaData gets *persisted* by an unrelated ALTER. The local system_schema write and the
peer schema announcement are the same migration mutation, so the locally-persisted leaked value is exactly
what would be disseminated. "Global dissemination" is only the transport; the root defect is observable on
one node via system_schema/DESCRIBE. No data rows needed (pure schema bug).

## Teardown
kubectl delete ns repro-12949 --wait=false
