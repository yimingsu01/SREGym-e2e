# CASSANDRA-16898 — Reproduction Evidence

## Bug (primary source: /tmp/jira_repro/CASSANDRA-16898.json)
- Summary: "The clustering order logic in Materialized view creation changed in 4.0"
- Description: Before 4.0, when a Materialized View was created with no `CLUSTERING ORDER`
  specified, the clustering order was based on the base table clustering order. In 4.0 this
  behaviour changed and the clustering order was defaulted to ASC for all columns.
- Components: Feature/Materialized Views
- fixVersions: 4.0.2
- Buggy version: 4.0.1   |   Fixed-control: 4.0.2 (exists, 4.0.2 <= 4.0 ceiling .20)

## Classifier hint vs reality (tag_correction)
- Hint: topology=1node, confidence=H, trigger="CREATE MATERIALIZED VIEW with no CLUSTERING ORDER
  on base table with DESC clustering -> MV defaults all columns to ASC instead of inheriting base order".
- Reality: ACCURATE. Single node, schema-definition (node-local) logic. tag_correction = none.

## Topology
- Two single-node Cassandra pods in namespace `repro-16898` (kind, context kind-kind):
  - `cass-buggy`  = cassandra:4.0.1
  - `cass-fixed`  = cassandra:4.0.2
- Identical config on both. Keyspace: `repro16898ks` (SimpleStrategy RF=1).

## Config gate handled
4.0 ships `enable_materialized_views: false`. Both pods launched with an in-place sed enabling it
BEFORE docker-entrypoint (identical on both, so the only variable is the version):
```
sed -i 's/^enable_materialized_views:.*/enable_materialized_views: true/' /etc/cassandra/cassandra.yaml
```
Confirmed post-start:
```
$ kubectl exec -n repro-16898 cass-buggy -- grep -i enable_materialized_views /etc/cassandra/cassandra.yaml
enable_materialized_views: true
$ kubectl exec -n repro-16898 cass-fixed -- grep -i enable_materialized_views /etc/cassandra/cassandra.yaml
enable_materialized_views: true
```
Exact release versions (system.local):
```
cass-buggy -> release_version 4.0.1
cass-fixed -> release_version 4.0.2
```

## Reproducer (identical CQL on both pods) — /tmp/repro-16898.cql
```
CREATE KEYSPACE IF NOT EXISTS repro16898ks WITH replication = {'class':'SimpleStrategy','replication_factor':1};

CREATE TABLE repro16898ks.base (
  pk int, ck int, v int,
  PRIMARY KEY (pk, ck)
) WITH CLUSTERING ORDER BY (ck DESC);     -- base clustering column ck is DESC

CREATE MATERIALIZED VIEW repro16898ks.mv AS
  SELECT pk, ck, v FROM repro16898ks.base
  WHERE pk IS NOT NULL AND ck IS NOT NULL AND v IS NOT NULL
  PRIMARY KEY (pk, v, ck);               -- NO 'WITH CLUSTERING ORDER' specified; ck stays a clustering col
```

Ground-truth query (run on each node):
```
SELECT column_name, kind, position, clustering_order
FROM system_schema.columns WHERE keyspace_name='repro16898ks' AND table_name='mv';
```

## RAW OUTPUT — BUGGY (cassandra:4.0.1)  [the misbehaviour]
CREATE succeeded (benign warning: "Materialized views are experimental ...").

Base table (control for inheritance source) — ck = desc:
```
 column_name | kind          | position | clustering_order
-------------+---------------+----------+------------------
          ck |    clustering |        0 |             desc
          pk | partition_key |        0 |             none
           v |       regular |       -1 |             none
```

MV `mv` ground truth — ck = **asc** (DID NOT inherit base DESC):
```
 column_name | kind          | position | clustering_order
-------------+---------------+----------+------------------
          ck |    clustering |        1 |              asc   <-- BUG: base ck is DESC, MV defaulted to ASC
          pk | partition_key |        0 |             none
           v |    clustering |        0 |              asc
```

DESCRIBE MATERIALIZED VIEW repro16898ks.mv (4.0.1):
```
 WITH CLUSTERING ORDER BY (v ASC, ck ASC)
```

## RAW OUTPUT — FIXED CONTROL (cassandra:4.0.2)  [identical workload, correct behaviour]
Base table identical (ck = desc). MV `mv` ground truth — ck = **desc** (inherited base order):
```
 column_name | kind          | position | clustering_order
-------------+---------------+----------+------------------
          ck |    clustering |        1 |             desc   <-- FIXED: MV inherits base ck DESC
          pk | partition_key |        0 |             none
           v |    clustering |        0 |              asc
```

DESCRIBE MATERIALIZED VIEW repro16898ks.mv (4.0.2):
```
 WITH CLUSTERING ORDER BY (v ASC, ck DESC)
```

## Discriminator (verbatim signature)
For inherited clustering column `ck` (base = DESC), identical CREATE MATERIALIZED VIEW:
- 4.0.1 (buggy): system_schema.columns clustering_order = **asc**   (DESCRIBE: `WITH CLUSTERING ORDER BY (v ASC, ck ASC)`)
- 4.0.2 (fixed): system_schema.columns clustering_order = **desc**  (DESCRIBE: `WITH CLUSTERING ORDER BY (v ASC, ck DESC)`)

`v` is ASC in both because `v` was a regular column in the base (no clustering order to inherit);
only the inherited clustering column `ck` differs — exactly matching the Jira description that the
4.0 regression defaulted clustering order to ASC instead of inheriting the base table order.

## Disposition: reproduced
Verbatim buggy signature captured; clean A/B against the fixed image (4.0.2).

## Teardown
Namespace repro-16898 deleted (kubectl delete ns repro-16898 --wait=false).
