# CASSANDRA-18760 — Reproduction Evidence Log

- **Issue**: CASSANDRA-18760 — "Backport CASSANDRA-16905 to older branches"
- **Components**: Cluster/Schema
- **Fix versions**: 3.0.30, 3.11.16, **4.0.12**
- **Buggy image**: `cassandra:4.0.11`  |  **Fixed control image**: `cassandra:4.0.12` (4.0.12 <= ceiling 4.0->20)
- **Topology**: single node (schema + local sstable; no ring needed) — matches classifier hint `1node`, confidence H.
- **Namespace**: `repro-18760`  |  **Keyspace**: `repro18760`
- **Date**: 2026-06-12

## Bug / reproducer extracted from the Jira body (ground truth)
The ticket backports CASSANDRA-16905, which adds a *guardrail* that BLOCKS re-adding a previously
dropped column with an incompatible type. The body describes the dangerous scenario:

> "Recently hit an un-recoverable situation in Cassandra 4.0.10 after dropping a 'map' column and
> adding it back as a 'blob', which caused corruption that neither offline nor online scrub could fix.
> When dropping a 'blob' column and attempting to add it back as a 'map' type, the operation is blocked..."

Two directions, only ONE is the bug:
- **map -> blob** = THE BUG. On 4.0.11 this re-add is NOT validated, succeeds silently, and produces
  on-disk corruption (the surviving map cells can no longer be serialized as a `blob` column). This is
  what 4.0.12 must block.
- **blob -> map** = red herring. Already blocked on both 4.0.11 and 4.0.12 by pre-existing validation.
  The `InvalidRequest ... incompatible with previous type blob` quoted in the Jira is THIS direction's
  block and does NOT discriminate versions — it was deliberately NOT used as the buggy signature.

Reproducer (map->blob): create a table with `col1 map<int,tinyint>`, write map cells with a far-future
timestamp (defeats DROP read-shadowing so the cells survive and get read under the new column),
`nodetool flush`, `ALTER ... DROP col1`, `ALTER ... ADD col1 blob`, then `SELECT *`.

## Tag correction
None. Classifier hints (topology=1node, trigger=map->blob re-add -> on-disk corruption unfixable by
scrub) match the body. The map->blob direction is the bug as tagged.

---

## BUGGY 4.0.11 — verbatim evidence

### Versions confirmed
```
$ kubectl exec -n repro-18760 cass-buggy -- cqlsh -e "SELECT release_version FROM system.local"
 release_version
-----------------
          4.0.11
```

### Setup (table t2, far-future-timestamp map cells survive the DROP)
```
$ kubectl exec -n repro-18760 cass-buggy -- cqlsh -e "
CREATE TABLE repro18760.t2 (pk int PRIMARY KEY, col1 map<int, tinyint>);
INSERT INTO repro18760.t2 (pk, col1) VALUES (1, {10:1, 20:2, 30:3}) USING TIMESTAMP 9223372036854775000;
INSERT INTO repro18760.t2 (pk, col1) VALUES (2, {40:4, 50:5}) USING TIMESTAMP 9223372036854775000;
SELECT * FROM repro18760.t2;"

 pk | col1
----+-----------------------
  1 | {10: 1, 20: 2, 30: 3}
  2 |        {40: 4, 50: 5}
(2 rows)

$ kubectl exec -n repro-18760 cass-buggy -- nodetool flush repro18760 t2     # ok
```

### THE BUG: map->blob re-add SUCCEEDS on 4.0.11 (missing guardrail)
```
$ kubectl exec -n repro-18760 cass-buggy -- cqlsh -e "ALTER TABLE repro18760.t2 DROP col1; ALTER TABLE repro18760.t2 ADD col1 blob;"
exit=0 (alter map->blob)        <-- NO error; the dangerous re-add is allowed
```
Contradictory schema state left behind (this is impossible on the fixed version):
```
$ kubectl exec -n repro-18760 cass-buggy -- cqlsh -e "DESCRIBE TABLE repro18760.t2"   | grep col1
    col1 blob                                                  <-- live column is now blob
$ kubectl exec -n repro-18760 cass-buggy -- cqlsh -e \
    "SELECT column_name,type FROM system_schema.dropped_columns WHERE keyspace_name='repro18760' AND table_name='t2'"
 column_name | type
        col1 | map<int, tinyint>                               <-- AND col1 also recorded as dropped map
```

### Corruption surfaces — client-visible ReadFailure
```
$ kubectl exec -n repro-18760 cass-buggy -- cqlsh -e "SELECT * FROM repro18760.t2;"
<stdin>:1:ReadFailure: Error from server: code=1300 [Replica(s) failed to execute read] message="Operation failed - received 0 responses and 1 failures: UNKNOWN from /10.244.3.40:7000" info={'consistency': 'ONE', 'required_responses': 1, 'received_responses': 0, 'failures': 1, 'error_code_map': {'10.244.3.40': '0x0000'}}
command terminated with exit code 2
```

### *** VERBATIM BUGGY SIGNATURE *** — server-side exception + frame (kubectl logs cass-buggy)
The surviving map (complex) cells cannot be serialized under the `blob` (simple) column:
```
ERROR [ReadStage-13] 2026-06-12 03:05:27,439 AbstractLocalAwareExecutorService.java:169 - Uncaught exception on thread Thread[ReadStage-13,10,main]
java.lang.AssertionError: col1
	at org.apache.cassandra.db.rows.UnfilteredSerializer.lambda$serializeRowBody$0(UnfilteredSerializer.java:244)
	at org.apache.cassandra.db.rows.UnfilteredSerializer.serializeRowBody(UnfilteredSerializer.java:237)
	at org.apache.cassandra.db.rows.UnfilteredSerializer.serialize(UnfilteredSerializer.java:205)
	at org.apache.cassandra.db.rows.UnfilteredSerializer.serialize(UnfilteredSerializer.java:137)
	at org.apache.cassandra.db.rows.UnfilteredSerializer.serialize(UnfilteredSerializer.java:125)
	at org.apache.cassandra.db.rows.UnfilteredRowIteratorSerializer.serialize(UnfilteredRowIteratorSerializer.java:146)
	at org.apache.cassandra.db.rows.UnfilteredRowIteratorSerializer.serialize(UnfilteredRowIteratorSerializer.java:95)
	at org.apache.cassandra.db.rows.UnfilteredRowIteratorSerializer.serialize(UnfilteredRowIteratorSerializer.java:80)
	at org.apache.cassandra.db.partitions.UnfilteredPartitionIterators$Serializer.serialize(UnfilteredPartitionIterators.java:308)
```

### "neither online nor offline scrub could fix" (corroborating, on table t — inserts before DROP)
On a first table `t` (default-timestamp inserts), scrub/compact run with exit=0 but SILENTLY DISCARD
the data instead of fixing it — `sstabledump` of the post-compaction Data.db shows the cells gone:
```
$ kubectl exec -n repro-18760 cass-buggy -- nodetool scrub  repro18760 t   # exit=0, no fix
$ kubectl exec -n repro-18760 cass-buggy -- nodetool compact repro18760 t   # exit=0
$ kubectl exec -n repro-18760 cass-buggy -- /opt/cassandra/tools/bin/sstabledump <Data.db>
  ... "rows":[ {"type":"row", ..., "cells":[]} ] ...     <-- map data permanently lost; scrub did not recover it
```

---

## FIXED 4.0.12 — A/B control (proves the fix; NOT the buggy signature)

### Identical map->blob sequence is BLOCKED by the CASSANDRA-16905 guardrail
```
$ kubectl exec -n repro-18760 cass-fixed -- cqlsh -e "SELECT release_version FROM system.local"  -> 4.0.12

$ kubectl exec -n repro-18760 cass-fixed -- cqlsh -e "
CREATE TABLE repro18760.t2 (pk int PRIMARY KEY, col1 map<int, tinyint>);
INSERT INTO repro18760.t2 (pk, col1) VALUES (1, {10:1, 20:2, 30:3}) USING TIMESTAMP 9223372036854775000;"
$ kubectl exec -n repro-18760 cass-fixed -- nodetool flush repro18760 t2          # ok
$ kubectl exec -n repro-18760 cass-fixed -- cqlsh -e "ALTER TABLE repro18760.t2 DROP col1;"
exit=0 (DROP)

$ kubectl exec -n repro-18760 cass-fixed -- cqlsh -e "ALTER TABLE repro18760.t2 ADD col1 blob;"
<stdin>:1:InvalidRequest: Error from server: code=2200 [Invalid query] message="Cannot re-add previously dropped column 'col1' of type blob, incompatible with previous type map<int, tinyint>"
command terminated with exit code 2
exit=2 (ADD blob BLOCKED on 4.0.12)
```
Resulting schema on the fixed node is consistent — `col1` stays dropped, is NOT live as blob, so the
corrupt-schema state (and the AssertionError) can never be reached:
```
$ kubectl exec -n repro-18760 cass-fixed -- cqlsh -e "DESCRIBE TABLE repro18760.t2"   # col1 absent from live cols
$ ... dropped_columns -> col1 | map<int, tinyint>    (and col1 NOT in the table definition)
```

### Confound check — the buggy signature is buggy-ONLY (verified, not assumed)
A `SELECT *` on the fixed node's t2 ALSO returns a ReadFailure (because the far-future-timestamp map cell
physically survives the DROP). I pulled the fixed node's server log to confirm this is NOT the same error:

```
$ kubectl logs -n repro-18760 cass-fixed | grep -A 12 -iE 'AssertionError|ERROR \[ReadStage'
ERROR [ReadStage-1] 2026-06-12 03:06:13,946 AbstractLocalAwareExecutorService.java:169 - Uncaught exception ...
java.lang.RuntimeException: java.lang.IllegalStateException: [col1] is not a subset of []
	at org.apache.cassandra.service.StorageProxy$DroppableRunnable.run(StorageProxy.java:2281)
	...
Caused by: java.lang.IllegalStateException: [col1] is not a subset of []
	at org.apache.cassandra.db.Columns$Serializer.encodeBitmap(Columns.java:593)
	at org.apache.cassandra.db.Columns$Serializer.serializeSubset(Columns.java:523)
	at org.apache.cassandra.db.rows.UnfilteredSerializer.serializeRowBody(UnfilteredSerializer.java:231)

$ kubectl logs -n repro-18760 cass-buggy | grep -c 'AssertionError: col1'   ->  2
$ kubectl logs -n repro-18760 cass-fixed | grep -c 'AssertionError: col1'   ->  0
```

DIFFERENT exception, DIFFERENT frame:
- **buggy 4.0.11**: `java.lang.AssertionError: col1` at `UnfilteredSerializer.java:244` (the column IS live
  as `blob` but its on-disk cells are complex/map -> serializer assertion fails). THIS is the corruption.
- **fixed 4.0.12**: `java.lang.IllegalStateException: [col1] is not a subset of []` at `Columns.java:593`
  — a benign artifact of the surviving dropped-map cell referencing a column that is correctly NO LONGER
  live (live set is empty `[]`). The fix blocked the re-add, so `col1` never became a live `blob`, the
  corrupt-schema state is unreachable, and `AssertionError: col1` count on the fixed node is 0.

So the load-bearing control result is twofold: (1) the fix BLOCKS the type-incompatible re-add at ALTER
time, so the table can never enter the live `col1 blob` + dropped `col1 map` corrupt state; and (2) the
buggy `AssertionError: col1` signature is verified buggy-only.

---

## Conclusion
- **Disposition: reproduced.**
- Buggy 4.0.11 allows the map->blob re-add (missing CASSANDRA-16905 guardrail) and the resulting on-disk
  corruption surfaces as a client-visible `ReadFailure` plus the server-side
  `java.lang.AssertionError: col1` in `UnfilteredSerializer.serializeRowBody` — and scrub/compact do not
  fix it (they silently drop the data).
- Fixed 4.0.12 blocks the identical operation at ALTER time with
  `InvalidRequest ... Cannot re-add previously dropped column 'col1' of type blob, incompatible with
  previous type map<int, tinyint>`, preventing the corruption entirely.
