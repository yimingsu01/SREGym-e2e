# CASSANDRA-13935 Reproduction Evidence Log

**Summary:** Indexes (and UDTs) creation should have `IF NOT EXISTS` on its String representation.
**Buggy version:** cassandra:3.11.8
**Fixed-control version:** cassandra:3.11.9 (fix landed in 3.0.23 / 3.11.9 / 4.0-beta3 / 4.0; 3.11.9 <= 3.11 ceiling 19)
**Components:** Feature/2i Index, Legacy/CQL
**Topology:** single node (HINT topology=1node CONFIRMED — schema.cql generation is local; no ring needed)
**Disposition:** REPRODUCED (clean A/B on the generated artifact)

## Bug mechanism (from Jira body — ground truth)
When a snapshot is taken (`nodetool snapshot`), Cassandra writes a `schema.cql` file capturing the table
DDL so it can be replayed on restore. The `CREATE TABLE` statement is emitted with `IF NOT EXISTS`, but
the accompanying `CREATE INDEX` statement for any secondary index is emitted WITHOUT `IF NOT EXISTS`.
Tables without 2i restore fine; tables WITH a secondary index "fail miserably" when the generated
schema.cql is replayed over an existing/partially-existing schema, because the unguarded `CREATE INDEX`
collides ("already exists"). The bug is in the String representation that snapshot writes — title:
"... should have IF NOT EXISTS on its String representation".

NOTE (non-idempotency, not a fresh-restore crash): a fresh single replay into an empty keyspace does NOT
error — `CREATE TABLE IF NOT EXISTS` + unguarded `CREATE INDEX` both succeed once. The defect is (1) the
literal missing `IF NOT EXISTS` token in the generated file, and (2) the resulting non-idempotent replay.

## Environment
- Existing kind cluster, context kind-kind, 4 nodes.
- Namespace created: `repro-13935` (isolated). Keyspace: `repro13935`.
- Two single-node pods deployed in the same namespace:
  - `cass-buggy`  = cassandra:3.11.8 (pinned to kind-worker2 where image was cached locally; Docker Hub
    429 rate-limit prevented pulling 3.11.8 fresh, image already present on kind-worker2)
  - `cass-fixed`  = cassandra:3.11.9 (image already present on kind-worker3)
- Verified release_version on each: 3.11.8 and 3.11.9 respectively.

## Reproducer steps (identical workload on both pods)
```
CREATE KEYSPACE repro13935 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE repro13935.table1 (id text PRIMARY KEY, content text, last_update_date date, last_update_date_time timestamp);
CREATE INDEX ON repro13935.table1 (last_update_date);   -- auto-named => table1_last_update_date_idx
```
Verify index exists (both pods):
```
SELECT index_name FROM system_schema.indexes WHERE keyspace_name='repro13935' AND table_name='table1';
 index_name
-----------------------------
 table1_last_update_date_idx
(1 rows)
```
Take snapshot (both pods):
```
nodetool snapshot -t snap1 repro13935
  -> Requested creating snapshot(s) for [repro13935] with snapshot name [snap1] and options {skipFlush=false}
  -> Snapshot directory: snap1
```

## PRIMARY EVIDENCE — generated schema.cql (the buggy artifact)

### BUGGY 3.11.8  (cass-buggy)
Path: /var/lib/cassandra/data/repro13935/table1-9e831360661611f19be82fd4a5644551/snapshots/snap1/schema.cql
Last line of generated file:
```
CREATE INDEX table1_last_update_date_idx ON repro13935.table1 (last_update_date);
```
(Table line above it IS guarded: `CREATE TABLE IF NOT EXISTS repro13935.table1 (...`.
 The index line is MISSING `IF NOT EXISTS`.)

### FIXED 3.11.9  (cass-fixed)
Path: /var/lib/cassandra/data/repro13935/table1-af95a690661611f1a9318d1296af06c4/snapshots/snap1/schema.cql
Last line of generated file:
```
CREATE INDEX IF NOT EXISTS table1_last_update_date_idx ON repro13935.table1 (last_update_date);
```
(Now correctly guarded with `IF NOT EXISTS` — the exact line the fix changed.)

**Discriminator is precisely the `CREATE INDEX` line; everything else in the two files is identical.**

## CORROBORATING IMPACT — replay generated schema.cql over the now-existing schema
Each pod replays its OWN generated file with `cqlsh -f <schema.cql>`:

### BUGGY 3.11.8 — FAILS (non-idempotent restore)
```
schema.cql:24:InvalidRequest: Error from server: code=2200 [Invalid query] message="Index table1_last_update_date_idx already exists"
command terminated with exit code 2   (replay rc=2)
```
(CREATE TABLE IF NOT EXISTS was skipped fine; the unguarded CREATE INDEX statement collided. This is the
reporter's "fails miserably" on restore. Note: the "schema.cql:24" prefix is cqlsh's own statement/line
counter and is not asserted to map exactly to a physical file line — the content discriminator above
stands on its own.)

### FIXED 3.11.9 — SUCCEEDS (idempotent restore)
```
(only deprecation warnings: "dclocal_read_repair_chance table option has been deprecated ...")
replay rc=0
```

NOTE on the replay error as a signature: the `Index ... already exists` text is generic and would also be
produced if you ran a bare `CREATE INDEX` twice in EITHER version — so it is logged here as IMPACT
evidence, not as the standalone proof. The standalone proof is the generated-file `CREATE INDEX` line
itself (present vs `IF NOT EXISTS`-guarded), which is the literal artifact the fix changed.

## VERBATIM BUGGY SIGNATURE (literal copy from buggy 3.11.8 generated schema.cql)
```
CREATE INDEX table1_last_update_date_idx ON repro13935.table1 (last_update_date);
```

## Tag correction
HINT topology=1node, trigger "snapshot table with secondary index + replay generated schema.cql ->
CREATE INDEX without IF NOT EXISTS fails on restore" — CONFIRMED correct. One nuance: the failure is a
non-idempotent re-apply (over existing schema), not a fresh-into-empty-keyspace crash; the core defect is
the missing `IF NOT EXISTS` token in the snapshot's generated CREATE INDEX string representation.

## Teardown
Deleted namespace repro-13935 (--wait=false). No other namespace touched.
