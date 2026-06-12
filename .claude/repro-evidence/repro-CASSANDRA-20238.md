# CASSANDRA-20238 — Reproduction Evidence Log

**Summary:** Correct the default behavior of compareTo() when comparing WIDE and STATIC PrimaryKeys
**Component:** Feature/SAI
**Buggy version:** cassandra:5.0.3
**Fixed control:** cassandra:5.0.4 (fixVersions = 5.0.4, 6.0-alpha1, 6.0; 5.0.4 <= 5.0 ceiling 8)
**Topology:** single node, RF=1 (matches classifier hint topology=1node, confidence=H)
**Disposition:** REPRODUCED (wrong query result — SAI returns a missing row)

## Tag correction
None. Body confirms single node, single-node fuzz test. Classifier hint trigger
("SAI index + UPDATE static/value + partition DELETE + UPDATE + flush + SELECT WHERE v0=.. AND pk0=.. ALLOW FILTERING -> missing row")
matches the simplified reproducer in the Jira body exactly.

## Reproducer extracted (from Jira body — the simplified in-JVM dtest)
The reporter shrank the original exotic-typed fuzz history to a minimal int-typed test:
- Table with composite partition key ((pk0,pk1), ck0), a static column `s1`, value `v0`.
- SAI index on the PARTITION-KEY column pk0 only (the v0 index is commented out in the test; reporter
  notes it does not matter; the static column is essential — removing it makes the bug vanish).
- UPDATE (sets s1,v0 at ck0=0) -> partition DELETE (pk0,pk1) -> UPDATE creating the row at ck0=1 -> flush.
- `SELECT * WHERE v0=1 AND pk0=0 ALLOW FILTERING` must return 1 row; the SAI path returns 0.

The dtest uses executeInternal + coordinator at CL.ALL on a 1-node cluster — equivalent to plain cqlsh
writes/reads on a single pod (RF=1). Reproduced via cqlsh on a single Cassandra pod in kind.

Write timestamps were PINNED (USING TIMESTAMP 1000 / 2000 / 3000) to guarantee the ordering
UPDATE < partition-DELETE < final-UPDATE, so a timestamp collision cannot delete the ck0=1 row "for the
right reason" — the discriminator is a plain (non-SAI) read that proves the row physically exists.

## Environment
- kind cluster, context kind-kind. Namespace: repro-20238 (created by me). Keyspace: repro20238_ks (unique).
- Two single-node pods deployed in parallel: cass-503 (cassandra:5.0.3, buggy), cass-504 (cassandra:5.0.4, fixed).
- Verified release_version: cass-503 -> 5.0.3, cass-504 -> 5.0.4.

## Schema (identical on both pods)
```
CREATE KEYSPACE IF NOT EXISTS repro20238_ks WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE repro20238_ks.tbl (pk0 int, pk1 int, ck0 int, s1 int static, v0 int, PRIMARY KEY ((pk0, pk1), ck0));
CREATE INDEX tbl_pk0 ON repro20238_ks.tbl(pk0) USING 'sai';
```
DESCRIBE INDEX on BOTH pods returned (schema parity confirmed):
```
CREATE CUSTOM INDEX tbl_pk0 ON repro20238_ks.tbl (pk0) USING 'sai';
```

## Workload (identical on both pods, pinned timestamps)
```
UPDATE repro20238_ks.tbl USING TIMESTAMP 1000 SET s1=0, v0=0 WHERE pk0=0 AND pk1=1 AND ck0=0;
DELETE FROM repro20238_ks.tbl USING TIMESTAMP 2000 WHERE pk0=0 AND pk1=1;
UPDATE repro20238_ks.tbl USING TIMESTAMP 3000 SET v0=1 WHERE pk0=0 AND pk1=1 AND ck0=1;
nodetool flush repro20238_ks tbl        # flush rc=0 on both
```

## RESULTS

### BUGGY 5.0.3 (cass-503) — RAW cqlsh OUTPUT
Discriminator — plain (non-SAI) read proves the row is physically present on disk:
```
$ cqlsh -e "SELECT * FROM repro20238_ks.tbl WHERE pk0=0 AND pk1=1;"

 pk0 | pk1 | ck0 | s1   | v0
-----+-----+-----+------+----
   0 |   1 |   1 | null |  1

(1 rows)
```
BUG — the SAI query misses that same row:
```
$ cqlsh -e "SELECT * FROM repro20238_ks.tbl WHERE v0=1 AND pk0=0 ALLOW FILTERING;"

 pk0 | pk1 | ck0 | s1 | v0
-----+-----+-----+----+----


(0 rows)
```
Re-ran the SAI query — stable, still (0 rows); plain read still returns the row. Reproducible.

### CONTROL 5.0.4 (cass-504) — RAW cqlsh OUTPUT (identical workload)
Plain read (row present):
```
$ cqlsh -e "SELECT * FROM repro20238_ks.tbl WHERE pk0=0 AND pk1=1;"

 pk0 | pk1 | ck0 | s1   | v0
-----+-----+-----+------+----
   0 |   1 |   1 | null |  1

(1 rows)
```
SAI query — CORRECT, returns the 1 row (bug fixed):
```
$ cqlsh -e "SELECT * FROM repro20238_ks.tbl WHERE v0=1 AND pk0=0 ALLOW FILTERING;"

 pk0 | pk1 | ck0 | s1   | v0
-----+-----+-----+------+----
   0 |   1 |   1 | null |  1

(1 rows)
```

## A/B verdict
Identical schema (same SAI index on pk0), identical pinned-timestamp workload, identical flush.
- 5.0.3 buggy: SAI query returns (0 rows) while the row demonstrably exists (plain read returns it).
- 5.0.4 fixed: SAI query returns the 1 row correctly.
This is the exact "Missing rows" symptom in the Jira report (the row with v0=true/1 at the surviving
clustering key is absent from the SAI ALLOW FILTERING result). Root cause per ticket: WIDE-vs-STATIC
PrimaryKey compareTo() default behavior in the SAI on-disk path after a partition delete + re-insert.

## Verbatim buggy signature
Buggy SAI result `(0 rows)` from `SELECT * FROM repro20238_ks.tbl WHERE v0=1 AND pk0=0 ALLOW FILTERING;`
on 5.0.3, while the plain read `SELECT * ... WHERE pk0=0 AND pk1=1;` returns the row `0 | 1 | 1 | null | 1`.

## Tooling findings
Two `CREATE KEYSPACE` / `CREATE TABLE` statements (issued one-per-cqlsh-invocation via kubectl exec)
intermittently returned client-side `OperationTimedOut` even though they committed server-side
(subsequent DESCRIBE/queries confirmed the objects existed). This is a cqlsh client default-timeout
artifact under `kubectl exec`, not a Cassandra defect and unrelated to this bug. RECORD ONLY — not fixed.

## Teardown
Namespace repro-20238 deleted with `kubectl delete ns repro-20238 --wait=false` after this log was written.
