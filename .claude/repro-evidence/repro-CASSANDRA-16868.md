# CASSANDRA-16868 — Reproduction Evidence Log

## Bug
**Summary:** Secondary indexes on primary key columns can miss some writes.
**Buggy version:** Cassandra 4.0.0
**Control (fixed):** Cassandra 4.0.1 (4.0.1 is in fixVersions; 4.0.1 <= 4.0 line ceiling 20 → A/B control valid)
**fixVersions:** 3.0.26, 3.11.12, 4.0.1, 4.1-alpha1, 4.1
**Components:** Feature/2i Index
**Classifier hint:** topology=1node, confidence=H, trigger = "CREATE INDEX on clustering key + INSERT + DELETE + UPDATE same row + SELECT by indexed col -> row not found". Hint MATCHES the Jira body. tag_correction = none.

## Mechanism (from Jira body)
An UPDATE that lands after a DELETE on the same primary-key row reuses the *LivenessInfo of the
previously deleted row* in `CassandraIndex.updateRow` (instead of `getPrimaryKeyIndexLiveness` as
`insertRow` does). Result: the row becomes LIVE again (the UPDATE writes a live `v` cell) but **no
index entry is ever created**, so a SELECT filtering on the indexed PK-component column returns nothing
even though the row exists.

## Exact reproducer (verbatim from Jira body, first example)
```sql
CREATE TABLE t (pk int, ck int, v int, PRIMARY KEY (pk, ck));
CREATE INDEX ON t(ck);
INSERT INTO t(pk, ck, v) VALUES (1, 2, 3); -- creates an index entry (right)
DELETE FROM t WHERE pk = 1 AND ck = 2;     -- deletes the previous index entry (right)
UPDATE t SET v = 3 WHERE pk = 1 AND ck = 2;-- doesn't create a new index entry (wrong)
SELECT * FROM t WHERE ck = 2;              -- doesn't find the row (wrong)
```

## Environment
- Existing kind cluster, context `kind-kind`, 4 nodes.
- Namespace created: `repro-16868`.
- Two single-node pods deployed concurrently:
  - `cass-buggy`  image `cassandra:4.0.0`  (10.244.1.25, kind-worker3)
  - `cass-fixed`  image `cassandra:4.0.1`  (10.244.2.23, kind-worker2)
- Keyspace: `repro16868` (SimpleStrategy RF=1). Table `repro16868.t`.

Version banners:
```
[cqlsh 6.0.0 | Cassandra 4.0.0 | CQL spec 3.4.5 | Native protocol v5]   <- cass-buggy
[cqlsh 6.0.0 | Cassandra 4.0.1 | CQL spec 3.4.5 | Native protocol v5]   <- cass-fixed
```

## Commands run (workload, identical on both pods)
```sql
CREATE KEYSPACE IF NOT EXISTS repro16868 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE repro16868.t (pk int, ck int, v int, PRIMARY KEY (pk, ck));
CREATE INDEX ON repro16868.t(ck);
INSERT INTO repro16868.t(pk, ck, v) VALUES (1, 2, 3);
DELETE FROM repro16868.t WHERE pk = 1 AND ck = 2;
UPDATE repro16868.t SET v = 3 WHERE pk = 1 AND ck = 2;
```
(executed in-memtable, no flush — exactly as the body's first example, which has no flush)

## Discriminator (the key disambiguation)
Two SELECTs run at the end on each node. A timestamp-shadowing artifact (DELETE and UPDATE colliding at
the same microsecond → delete wins) would make BOTH return empty. The bug makes ONLY the indexed lookup
return empty while the PK lookup returns the live row.

### BUGGY 4.0.0 — kubectl exec -n repro-16868 cass-buggy -- cqlsh
```
>>> PK lookup (bypasses index): SELECT * FROM repro16868.t WHERE pk=1 AND ck=2;

 pk | ck | v
----+----+---
  1 |  2 | 3

(1 rows)

>>> Indexed lookup (uses 2i):  SELECT * FROM repro16868.t WHERE ck=2;

 pk | ck | v
----+----+---


(0 rows)
```
**=> Row is LIVE (PK lookup returns (1,2,3)) but the secondary index returns (0 rows). BUG REPRODUCED.**

### FIXED 4.0.1 — kubectl exec -n repro-16868 cass-fixed -- cqlsh  (A/B CONTROL, identical workload)
```
>>> PK lookup (bypasses index): SELECT * FROM repro16868.t WHERE pk=1 AND ck=2;

 pk | ck | v
----+----+---
  1 |  2 | 3

(1 rows)

>>> Indexed lookup (uses 2i):  SELECT * FROM repro16868.t WHERE ck=2;

 pk | ck | v
----+----+---
  1 |  2 | 3

(1 rows)
```
**=> Both lookups return the row. The fix (using getPrimaryKeyIndexLiveness in updateRow) creates the
index entry correctly. Control does NOT misbehave.**

## Verbatim buggy signature
The most-telling buggy line (indexed lookup on 4.0.0, while the PK lookup and the 4.0.1 control both
return the row):
```
SELECT * FROM repro16868.t WHERE ck=2;
 pk | ck | v
----+----+---

(0 rows)
```
This is a WRONG-RESULT bug (silent data-correctness / missing index entry), not an exception — there is
no stack trace by design. The signature is meaningful only in contrast: PK lookup = (1,2,3), control 4.0.1
indexed lookup = (1,2,3).

## Disposition: reproduced
Verbatim wrong-result signature captured; A/B control with cassandra:4.0.1 confirms the fix. Row proven
LIVE via PK path, UNINDEXED via the index path — exactly the CASSANDRA-16868 mechanism.

## Teardown
`kubectl delete ns repro-16868 --wait=false` (executed after writing this log).
