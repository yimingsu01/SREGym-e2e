# CASSANDRA-17266 Reproduction Evidence

**Summary:** DESCRIBE KEYSPACE / MATERIALIZED VIEW generates invalid CQL for views
**Buggy version:** cassandra:4.0.3  |  **Fixed control:** cassandra:4.0.4
**fixVersions:** 4.0.4, 4.1-alpha1, 4.1  |  **Components:** CQL/Syntax
**Topology:** single node (server-side DESCRIBE generation)  |  **Disposition:** REPRODUCED
**Namespace:** repro-17266  |  **Keyspace:** repro17266_ks

## Bug mechanism (from Jira body)
Materialized views do not allow the `default_time_to_live` option (CASSANDRA-12868). But if an MV
is created without it, `DESCRIBE MATERIALIZED VIEW` emits CQL that *includes* `default_time_to_live = 0`.
Re-creating the view from that DESCRIBE output then fails. The defect is in DESCRIBE output generation
(`TableParams.appendCqlTo` / `TableMetadata.appendTableOptions`), NOT in the validation that rejects the
option. Because the rejecting validation exists in BOTH 4.0.3 and 4.0.4, the correct A/B control is a
round-trip on each version's OWN DESCRIBE output: the discriminator is the presence/absence of the
`default_time_to_live` line, not the replay error.

## Environment
- Existing kind cluster, context kind-kind, 4 nodes. Two pods in namespace repro-17266:
  `cass` (cassandra:4.0.3, buggy) and `cass-fixed` (cassandra:4.0.4, fixed).
- MV is disabled by default in 4.0 ("Materialized views are disabled. Enable in cassandra.yaml to use.").
  Enabled via appending `enable_materialized_views: true` to /etc/cassandra/cassandra.yaml in the pod
  command before `docker-entrypoint.sh cassandra -f`. This config-gating is REQUIRED to reach the path
  and is identical on both pods, so it does not affect the A/B comparison.

SHOW VERSION:
    [cqlsh 6.0.0 | Cassandra 4.0.3 | CQL spec 3.4.5 | Native protocol v5]   (cass, buggy)
    [cqlsh 6.0.0 | Cassandra 4.0.4 | CQL spec 3.4.5 | Native protocol v5]   (cass-fixed)

## Reproducer workload (exact, from Jira body; keyspace renamed for isolation)
```
CREATE KEYSPACE IF NOT EXISTS repro17266_ks WITH replication = {'class': 'SimpleStrategy', 'replication_factor': '1'};
USE repro17266_ks;
CREATE TABLE IF NOT EXISTS test_table(
  id text, date text, col1 text, col2 text,
  PRIMARY KEY(id,date)
) WITH default_time_to_live = 60 AND CLUSTERING ORDER BY (date DESC);
CREATE MATERIALIZED VIEW IF NOT EXISTS test_view AS
SELECT id, date, col1 FROM test_table
WHERE id IS NOT NULL AND date IS NOT NULL
PRIMARY KEY(id, date);
```
Note the parent table has `default_time_to_live = 60`; the MV was created with NO TTL option.

================================================================================
## BUGGY 4.0.3 — DESCRIBE MATERIALIZED VIEW repro17266_ks.test_view
================================================================================
```
CREATE MATERIALIZED VIEW repro17266_ks.test_view AS
    SELECT id, date, col1
    FROM repro17266_ks.test_table
    WHERE id IS NOT NULL AND date IS NOT NULL
    PRIMARY KEY (id, date)
 WITH CLUSTERING ORDER BY (date DESC)
    AND additional_write_policy = '99p'
    AND bloom_filter_fp_chance = 0.01
    AND caching = {'keys': 'ALL', 'rows_per_partition': 'NONE'}
    AND cdc = false
    AND comment = ''
    AND compaction = {'class': 'org.apache.cassandra.db.compaction.SizeTieredCompactionStrategy', 'max_threshold': '32', 'min_threshold': '4'}
    AND compression = {'chunk_length_in_kb': '16', 'class': 'org.apache.cassandra.io.compress.LZ4Compressor'}
    AND crc_check_chance = 1.0
    AND default_time_to_live = 0                <-- BUG: invalid for an MV
    AND extensions = {}
    AND gc_grace_seconds = 864000
    AND max_index_interval = 2048
    AND memtable_flush_period_in_ms = 0
    AND min_index_interval = 128
    AND read_repair = 'BLOCKING'
    AND speculative_retry = '99p';
```
`grep default_time_to_live` -> line 15: `    AND default_time_to_live = 0`  (PRESENT — the defect)

### BUGGY round-trip (DROP + replay the exact DESCRIBE output)
```
DROP MATERIALIZED VIEW repro17266_ks.test_view        -> RC=0
<replay captured CREATE ...>
/tmp/buggy_replay.cql:24:InvalidRequest: Error from server: code=2200 [Invalid query] message="Cannot set default_time_to_live for a materialized view. Data in a materialized view always expire at the same time than the corresponding data in the parent table."
REPLAY RC=2
```
=> DESCRIBE produced CQL that the server itself rejects. Round-trip BROKEN. Matches Jira body verbatim.

================================================================================
## FIXED 4.0.4 — DESCRIBE MATERIALIZED VIEW repro17266_ks.test_view (CONTROL)
================================================================================
```
CREATE MATERIALIZED VIEW repro17266_ks.test_view AS
    SELECT id, date, col1
    FROM repro17266_ks.test_table
    WHERE id IS NOT NULL AND date IS NOT NULL
    PRIMARY KEY (id, date)
 WITH CLUSTERING ORDER BY (date DESC)
    AND additional_write_policy = '99p'
    AND bloom_filter_fp_chance = 0.01
    AND caching = {'keys': 'ALL', 'rows_per_partition': 'NONE'}
    AND cdc = false
    AND comment = ''
    AND compaction = {...SizeTieredCompactionStrategy...}
    AND compression = {...LZ4Compressor...}
    AND crc_check_chance = 1.0
    AND extensions = {}                          <-- default_time_to_live LINE ABSENT (fixed)
    AND gc_grace_seconds = 864000
    AND max_index_interval = 2048
    AND memtable_flush_period_in_ms = 0
    AND min_index_interval = 128
    AND read_repair = 'BLOCKING'
    AND speculative_retry = '99p';
```
`grep default_time_to_live` -> (no match) — CORRECT, option omitted for MVs.

### FIXED round-trip (DROP + replay the exact DESCRIBE output)
```
DROP MATERIALIZED VIEW repro17266_ks.test_view        -> RC=0
<replay captured CREATE ...>
Warnings : Materialized views are experimental and are not recommended for production use.
REPLAY RC=0
SELECT view_name FROM system_schema.views WHERE keyspace_name='repro17266_ks' -> test_view (1 rows)
```
=> DESCRIBE output is valid CQL; view recreated cleanly. Bug fixed in 4.0.4.

================================================================================
## VERDICT
================================================================================
REPRODUCED. The primary defect is the line **`AND default_time_to_live = 0`** appearing inside the
`CREATE MATERIALIZED VIEW` block of `DESCRIBE MATERIALIZED VIEW` output on 4.0.3. It is absent on 4.0.4.
Corroboration: replaying the buggy DESCRIBE output is rejected by the server with
"Cannot set default_time_to_live for a materialized view...", while the fixed output replays successfully.

tag_correction: none. Hint (topology=1node, confidence=H, trigger = CREATE MV no TTL + DESCRIBE MV emits
default_time_to_live=0 -> re-CREATE fails) matched the Jira body exactly.

tooling_findings: In cassandra:4.0.x, materialized views are gated off by default
(enable_materialized_views: false). A bug reproducer that needs an MV must set
enable_materialized_views: true in cassandra.yaml or CREATE MATERIALIZED VIEW fails with
"Materialized views are disabled. Enable in cassandra.yaml to use." Also, the first MV-related DDL on a
freshly-started 4.0 node can exceed the default cqlsh client timeout (OperationTimedOut) even though the
DDL actually applies server-side; use --request-timeout and/or verify via system_schema. No repo/tooling
files were modified.
