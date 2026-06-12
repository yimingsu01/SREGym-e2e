# CASSANDRA-12734 — Reproduction Evidence Log

- **Bug**: "Materialized View schema file for snapshots created as tables"
- **Body (ground truth)**: "The materialized view schema file that gets created and stored with the
  sstables is created as a table instead of a materialized view." (snapshot `schema.cql`)
- **Components**: Feature/Materialized Views, Legacy/Tools
- **fixVersions**: 3.0.26, 3.11.12, 4.0.2, 4.1-alpha1, 4.1
- **Buggy image under test**: `cassandra:4.0.1`  (A/B fixed control: `cassandra:4.0.2`)
- **Topology**: 1 node (classifier hint = 1node, CONFIRMED correct)
- **Namespace**: `repro-12734`  | **Keyspace**: `ks12734`
- **Disposition**: **NOT-REPRODUCIBLE** — the body's mechanism does not fire on cassandra:4.0.1.

## Reproducer extracted from body
A table with a materialized view; take a snapshot; the MV's snapshot schema file should (per the bug)
be wrongly written as `CREATE TABLE`. Exercised BOTH snapshot paths:
1. explicit `nodetool snapshot` (the `SchemaCQLHelper.getTableMetadataAsCQL` path), and
2. auto-snapshot on `DROP MATERIALIZED VIEW` (the `Schema.dropView` reorder path from the fix commit).

## Config gate (important)
Cassandra 4.0 ships with Materialized Views DISABLED by default. Both pods were started with
`enable_materialized_views: true` baked into the container command. Verified applied:
```
$ kubectl exec -n repro-12734 cass        -- grep -i materialized_views /etc/cassandra/cassandra.yaml
enable_materialized_views: true
$ kubectl exec -n repro-12734 cass-fixed  -- grep -i materialized_views /etc/cassandra/cassandra.yaml
enable_materialized_views: true
$ kubectl exec -n repro-12734 cass        -- nodetool version   ->  ReleaseVersion: 4.0.1
$ kubectl exec -n repro-12734 cass-fixed  -- nodetool version   ->  ReleaseVersion: 4.0.2
```
`CREATE MATERIALIZED VIEW` succeeded (system_schema.views shows ks12734 | mv_by_v), confirming the
MV code path was actually reached — the snapshot dump genuinely operated on a view.

## Workload (identical on both pods)
```
CREATE KEYSPACE ks12734 WITH replication={'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE ks12734.base (id int PRIMARY KEY, v int);
CREATE MATERIALIZED VIEW ks12734.mv_by_v AS SELECT * FROM ks12734.base
  WHERE v IS NOT NULL AND id IS NOT NULL PRIMARY KEY (v, id);
INSERT INTO ks12734.base (id,v) VALUES (1,10);   INSERT INTO ks12734.base (id,v) VALUES (2,20);
nodetool flush ks12734
nodetool snapshot -t t1 ks12734
# then, on buggy pod only, to exercise the dropView auto-snapshot path:
DROP MATERIALIZED VIEW ks12734.mv_by_v;     # auto_snapshot: true (verified)
```

## OBSERVED OUTPUT — BUGGY 4.0.1 (the smoking gun is ABSENT)

### Path 1: explicit `nodetool snapshot -t t1` — the view's snapshot schema file
File: `/var/lib/cassandra/data/ks12734/mv_by_v-61bba9e0660e11f1bc522be06e6ab383/snapshots/t1/schema.cql`
```
CREATE MATERIALIZED VIEW IF NOT EXISTS ks12734.mv_by_v AS
    SELECT *
    FROM ks12734.base
    WHERE v IS NOT NULL AND id IS NOT NULL
    PRIMARY KEY (v, id)
 WITH ID = 61bba9e0-660e-11f1-bc52-2be06e6ab383
    AND CLUSTERING ORDER BY (id ASC)
    ...
```
==> CORRECT. NOT a `CREATE TABLE`. The bug's symptom does not appear.

### Path 2: auto-snapshot from `DROP MATERIALIZED VIEW` (Schema.dropView path)
File: `/var/lib/cassandra/data/ks12734/mv_by_v-61bba9e0660e11f1bc522be06e6ab383/snapshots/dropped-1781234830768-mv_by_v/schema.cql`
```
CREATE MATERIALIZED VIEW IF NOT EXISTS ks12734.mv_by_v AS
    SELECT *
    FROM ks12734.base
    WHERE v IS NOT NULL AND id IS NOT NULL
    PRIMARY KEY (v, id)
 WITH ID = 61bba9e0-660e-11f1-bc52-2be06e6ab383
    AND CLUSTERING ORDER BY (id ASC)
    ...
```
==> CORRECT. NOT a `CREATE TABLE`. (The `dropped-<ts>-mv_by_v` dir proves the DROP ran and
    auto-snapshotted; the `DROP EXIT: 1` seen in the run was grep's exit code, not a cqlsh error.)

(The base table's snapshot schema.cql correctly reads `CREATE TABLE IF NOT EXISTS ks12734.base` — expected.)

## A/B CONTROL — FIXED 4.0.2 (identical correct output)
File: `/var/lib/cassandra/data/ks12734/mv_by_v-70fab590660e11f1869a5313ab6bcf48/snapshots/t1/schema.cql`
```
CREATE MATERIALIZED VIEW IF NOT EXISTS ks12734.mv_by_v AS
    SELECT *
    FROM ks12734.base
    WHERE v IS NOT NULL AND id IS NOT NULL
    PRIMARY KEY (v, id)
 WITH ID = 70fab590-660e-11f1-869a-5313ab6bcf48
    AND CLUSTERING ORDER BY (id ASC)
    ...
```
==> Byte-identical (modulo table UUID) to the 4.0.1 output. There is NO A/B difference.

## Root cause of non-reproduction (why 4.0.1 is already correct)
- The fix commit `67eb22ec9d588c9f984d13c0ffd703a14181f775` modifies
  `ColumnFamilyStoreCQLHelper.getCFMetadataAsCQL()` and uses the `CFMetaData` type, adding an
  `if (metadata.isView())` branch. These are the **3.0 / 3.11 branch** class/type names.
- The 4.0 line renamed these to `SchemaCQLHelper` / `TableMetadata`, and that 4.0 code path was
  **already materialized-view-aware from 4.0.0 GA**. Confirmed by jar inspection on the buggy image:
```
$ kubectl exec -n repro-12734 cass -- sh -c 'J=/opt/cassandra/lib/apache-cassandra-4.0.1.jar;
    echo SchemaCQLHelper:; grep -a -c "SchemaCQLHelper" "$J";
    echo ColumnFamilyStoreCQLHelper:; grep -a -c "ColumnFamilyStoreCQLHelper" "$J"'
SchemaCQLHelper: 2
ColumnFamilyStoreCQLHelper: 0          <-- the class the fix patches does not exist in 4.0.x
```
- Neither 4.0.1 nor 4.0.2 CHANGES.txt lists CASSANDRA-12734 (the 4.0.x ticket entry for the snapshot
  schema dump is tracked separately / the 4.0 path never had the table-vs-view defect). 4.0.1 header
  = `4.0.1` (top entry CASSANDRA-16873); 4.0.2 header = `4.0.2` (top entry CASSANDRA-16894).
- Net: the table-vs-view snapshot-schema defect described in the body is a 3.0/3.11-era bug. On the
  ASSIGNED buggy image cassandra:4.0.1, the symptom is already absent.

## Verbatim signature (the OBSERVED, CORRECT line — demonstrates the symptom is ABSENT)
`CREATE MATERIALIZED VIEW IF NOT EXISTS ks12734.mv_by_v AS`
(from 4.0.1 snapshot file mv_by_v-61bba9e0660e11f1bc522be06e6ab383/snapshots/t1/schema.cql)

## Teardown
`kubectl delete ns repro-12734 --wait=false`
