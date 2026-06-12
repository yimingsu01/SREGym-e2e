# CASSANDRA-19747 — Invalid schema.cql created by snapshot after dropping more than one field

- **Buggy version:** cassandra:4.1.5
- **Fixed control:** cassandra:4.1.6 (fixVersions: 4.0.14, 4.1.6, 5.0-rc1, 6.0-alpha1, 6.0; 4.1 ceiling=11 so 4.1.6 control is valid)
- **Component:** Local/Snapshots
- **Topology:** 1 node (matches classifier hint; body uses single `docker run`). tag_correction = none.
- **Namespace:** repro-19747 (kind-kind), keyspace `repro19747`
- **Disposition:** REPRODUCED (verbatim buggy signature + A/B control)

## Reproducer extracted from Jira body
After dropping >= 2 columns via `ALTER TABLE ... DROP (col2, col3)`, the `schema.cql` emitted by
`nodetool snapshot` omits the comma between the two remaining column definitions, producing invalid CQL.
Body's expected vs actual: actual is missing the comma after `field2 text`. This breaks schema restore
from a snapshot backup.

Steps (mirrors body exactly, unique keyspace for isolation):
1. CREATE KEYSPACE repro19747 (SimpleStrategy rf=1) + CREATE TABLE testtable (field1 PK, field2, field3)
2. ALTER TABLE repro19747.testtable DROP (field2, field3)
3. nodetool snapshot -t my_snapshot repro19747
4. find /var/lib/cassandra/data/repro19747 -name schema.cql -exec cat {} +

## Commands + raw output

### Deploy (both pods in ns repro-19747)
- pod `cass`       -> cassandra:4.1.5 (buggy)
- pod `cass-fixed` -> cassandra:4.1.6 (fixed control)
Both reached Ready + CQL responsive (`SELECT now() FROM system.local`).

### BUGGY 4.1.5 — schema.cql (verbatim)
```
$ kubectl exec -n repro-19747 cass -- cqlsh -e "CREATE KEYSPACE IF NOT EXISTS repro19747 WITH replication = {'class': 'SimpleStrategy', 'replication_factor': '1'}; CREATE TABLE IF NOT EXISTS repro19747.testtable (field1 text PRIMARY KEY,field2 text,field3 text);"
$ kubectl exec -n repro-19747 cass -- cqlsh -e "ALTER TABLE repro19747.testtable DROP (field2, field3);"
$ kubectl exec -n repro-19747 cass -- nodetool snapshot -t my_snapshot repro19747
Requested creating snapshot(s) for [repro19747] with snapshot name [my_snapshot] and options {skipFlush=false}
Snapshot directory: my_snapshot
$ kubectl exec -n repro-19747 cass -- sh -c "find /var/lib/cassandra/data/repro19747 -name schema.cql -exec cat {} +"
CREATE TABLE IF NOT EXISTS repro19747.testtable (
    field1 text PRIMARY KEY,
    field2 text
    field3 text
) WITH ID = 555eddf0-660b-11f1-a8a5-9bea21a7aaf0
    AND additional_write_policy = '99p'
    AND bloom_filter_fp_chance = 0.01
    AND caching = {'keys': 'ALL', 'rows_per_partition': 'NONE'}
    AND cdc = false
    AND comment = ''
    AND compaction = {'class': 'org.apache.cassandra.db.compaction.SizeTieredCompactionStrategy', 'max_threshold': '32', 'min_threshold': '4'}
    AND compression = {'chunk_length_in_kb': '16', 'class': 'org.apache.cassandra.io.compress.LZ4Compressor'}
    AND memtable = 'default'
    AND crc_check_chance = 1.0
    AND default_time_to_live = 0
    AND extensions = {}
    AND gc_grace_seconds = 864000
    AND max_index_interval = 2048
    AND memtable_flush_period_in_ms = 0
    AND min_index_interval = 128
    AND read_repair = 'BLOCKING'
    AND speculative_retry = '99p';
ALTER TABLE repro19747.testtable DROP field2 USING TIMESTAMP 1781233431533000;
ALTER TABLE repro19747.testtable DROP field3 USING TIMESTAMP 1781233431533001;
```

VERBATIM BUGGY SIGNATURE (missing comma — `field2 text` has no trailing comma):
```
    field2 text
    field3 text
```

### CONSEQUENCE — feeding the malformed CREATE TABLE back fails (restore breaks)
```
$ kubectl exec -n repro-19747 cass -- cqlsh -e "CREATE TABLE IF NOT EXISTS repro19747.testtable2 (
    field1 text PRIMARY KEY,
    field2 text
    field3 text
);"
<stdin>:1:SyntaxException: line 4:4 no viable alternative at input 'field3' (... text PRIMARY KEY,    field2 [text]    field3...)
command terminated with exit code 2
```
The generated schema.cql cannot be replayed — exactly the "restore fails" symptom from the body.

### CONTROL — FIXED 4.1.6 — schema.cql (verbatim, identical workload)
```
$ kubectl exec -n repro-19747 cass-fixed -- cqlsh -e "CREATE KEYSPACE ...; CREATE TABLE ... (field1 text PRIMARY KEY,field2 text,field3 text);"
$ kubectl exec -n repro-19747 cass-fixed -- cqlsh -e "ALTER TABLE repro19747.testtable DROP (field2, field3);"
$ kubectl exec -n repro-19747 cass-fixed -- nodetool snapshot -t my_snapshot repro19747
$ kubectl exec -n repro-19747 cass-fixed -- sh -c "find /var/lib/cassandra/data/repro19747 -name schema.cql -exec cat {} +" | head -8
CREATE TABLE IF NOT EXISTS repro19747.testtable (
    field1 text PRIMARY KEY,
    field2 text,
    field3 text
) WITH ID = 5f66c4c0-660b-11f1-b6c8-fb41eb4c4f49
    AND additional_write_policy = '99p'
    AND bloom_filter_fp_chance = 0.01
    AND caching = {'keys': 'ALL', 'rows_per_partition': 'NONE'}
```
On 4.1.6 the comma IS present (`field2 text,`) — valid CQL. Bug fixed.

## Conclusion
A/B confirmed: 4.1.5 emits malformed (comma-less) schema.cql after multi-column DROP; 4.1.6 emits valid
CQL with the same workload. The malformed CQL fails to parse on replay (SyntaxException), confirming the
backup/restore breakage. DISPOSITION = reproduced.

## Teardown
`kubectl delete ns repro-19747 --wait=false`
