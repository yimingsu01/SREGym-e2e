# CASSANDRA-16372 — Reproduction Evidence

**Summary:** Import from csv of empty strings in list fails with a ParseError: "Empty values are not allowed,  given up without retries"
**Component:** Tool/cqlsh
**Buggy version:** cassandra:3.11.9
**Control (fixed) version:** cassandra:3.11.10 (3.11.10 is in the issue's fixVersions; 10 <= 3.11 ceiling 19)
**fixVersions (from Jira):** 3.0.24, 3.11.10, 4.0-rc1, 4.0
**Topology:** single node (matches classifier hint topology=1node). No ring needed — this is a client-side cqlsh COPY/CSV bug.
**Disposition:** reproduced
**Namespace:** repro-16372 (kind-kind). Keyspace: repro_16372 (unique, isolated).

## Reproducer extracted from Jira body
1. CREATE TABLE with a `list<text>` column.
2. INSERT a row whose list contains an empty-string element: `['But if you now try to wash your hands,', '']`.
3. `COPY <table> TO 'file.csv'` (export) — succeeds, 1 row exported.
4. `TRUNCATE` the table.
5. `COPY <table> FROM 'file.csv'` (import) — FAILS with ParseError, the row is dropped.
6. Final `SELECT` returns 0 rows => silent data loss / corruption.

This exactly matches the classifier hint trigger. tag_correction: none.

## Environment
- Context: kind-kind, 4 nodes.
- Two single-node pods co-located in namespace `repro-16372`:
  - `cass`        = cassandra:3.11.9  (buggy)
  - `cass-fixed`  = cassandra:3.11.10 (control)
- Pod template: MAX_HEAP_SIZE=1024M, HEAP_NEWSIZE=256M, GossipingPropertyFileSnitch, dc1.
- Both reached CQL readiness (`SELECT now() FROM system.local` answered).

## BUGGY RUN (cassandra:3.11.9) — single cqlsh session via heredoc, absolute CSV path /tmp/ctm.csv

Command:
```
kubectl exec -i -n repro-16372 cass -- cqlsh <<'EOF'
CREATE KEYSPACE IF NOT EXISTS repro_16372 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE repro_16372.test_1 ( uid uuid PRIMARY KEY, texts list<text> );
INSERT INTO repro_16372.test_1 (uid, texts) VALUES (833fee3f-d4f9-418b-9387-84ac2cda5cb7, ['But if you now try to wash your hands,', '']);
SELECT * FROM repro_16372.test_1;
COPY repro_16372.test_1 (uid, texts) TO '/tmp/ctm.csv';
TRUNCATE TABLE repro_16372.test_1;
COPY repro_16372.test_1 (uid, texts) FROM '/tmp/ctm.csv';
SELECT * FROM repro_16372.test_1;
EOF
```

RAW OUTPUT (verbatim):
```
 uid                                  | texts
--------------------------------------+------------------------------------------------
 833fee3f-d4f9-418b-9387-84ac2cda5cb7 | ['But if you now try to wash your hands,', '']

(1 rows)
Using 16 child processes

Starting copy of repro_16372.test_1 with columns [uid, texts].
Processed: 1 rows; Rate:       5 rows/s; Avg. rate:       5 rows/s
1 rows exported to 1 files in 0.217 seconds.
Using 16 child processes

Starting copy of repro_16372.test_1 with columns [uid, texts].
<stdin>:8:Failed to import 1 rows: ParseError - Failed to parse ['But if you now try to wash your hands,', ''] : Empty values are not allowed,  given up without retries
<stdin>:8:Failed to process 1 rows; failed rows written to import_repro_16372_test_1.err
Processed: 1 rows; Rate:       3 rows/s; Avg. rate:       3 rows/sProcessed: 1 rows; Rate:       2 rows/s; Avg. rate:       3 rows/s
1 rows imported from 1 files in 0.390 seconds (0 skipped).

 uid | texts
-----+-------


(0 rows)
command terminated with exit code 2
```

### VERBATIM BUGGY SIGNATURE (the single most-telling line):
```
<stdin>:8:Failed to import 1 rows: ParseError - Failed to parse ['But if you now try to wash your hands,', ''] : Empty values are not allowed,  given up without retries
```
(Note: double space before "given up", matching the Jira body exactly.)

### Corroborating symptoms:
- Self-contradictory COPY FROM block: "Failed to import 1 rows" alongside "1 rows imported from 1 files in 0.390 seconds (0 skipped)".
- Final `SELECT * FROM repro_16372.test_1;` returns **0 rows** — the row that was present and exported is now lost. This is the data-corruption / silent-loss symptom the Jira flags in red.

## CONTROL RUN (cassandra:3.11.10) — IDENTICAL workload

Command (identical to buggy, against cass-fixed; keyspace pre-dropped to start clean):
```
kubectl exec -i -n repro-16372 cass-fixed -- cqlsh <<'EOF'
CREATE KEYSPACE repro_16372 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE repro_16372.test_1 ( uid uuid PRIMARY KEY, texts list<text> );
INSERT INTO repro_16372.test_1 (uid, texts) VALUES (833fee3f-d4f9-418b-9387-84ac2cda5cb7, ['But if you now try to wash your hands,', '']);
SELECT * FROM repro_16372.test_1;
COPY repro_16372.test_1 (uid, texts) TO '/tmp/ctm.csv';
TRUNCATE TABLE repro_16372.test_1;
COPY repro_16372.test_1 (uid, texts) FROM '/tmp/ctm.csv';
SELECT * FROM repro_16372.test_1;
EOF
```

RAW OUTPUT (verbatim):
```
 uid                                  | texts
--------------------------------------+------------------------------------------------
 833fee3f-d4f9-418b-9387-84ac2cda5cb7 | ['But if you now try to wash your hands,', '']

(1 rows)
Using 16 child processes

Starting copy of repro_16372.test_1 with columns [uid, texts].
Processed: 1 rows; Rate:       9 rows/s; Avg. rate:       9 rows/s
1 rows exported to 1 files in 0.140 seconds.
Using 16 child processes

Starting copy of repro_16372.test_1 with columns [uid, texts].
Processed: 1 rows; Rate:       4 rows/s; Avg. rate:       4 rows/sProcessed: 1 rows; Rate:       2 rows/s; Avg. rate:       3 rows/s
1 rows imported from 1 files in 0.383 seconds (0 skipped).

 uid                                  | texts
--------------------------------------+------------------------------------------------
 833fee3f-d4f9-418b-9387-84ac2cda5cb7 | ['But if you now try to wash your hands,', '']

(1 rows)
===CONTROL EXIT:0===
```

CSV content on control (identical export to buggy):
```
833fee3f-d4f9-418b-9387-84ac2cda5cb7,"['But if you now try to wash your hands,', '']"
```

### Control conclusion:
On 3.11.10 the IDENTICAL workload: NO ParseError, COPY FROM reports "1 rows imported ... (0 skipped)", and the final SELECT returns the row **fully intact including the empty-string list element**. Exit 0. The fix (cqlsh CSV import of empty collection elements) resolves the bug. The exported CSV is byte-identical between versions, proving the divergence is purely in COPY FROM parsing.

## Notes on confounds (transparency)
- One control attempt initially hung at COPY TO due to cqlsh's Python multiprocessing (16 workers on a 2-core pod) — an infra flake, NOT the bug. Killed the stuck workers and re-ran cleanly; the clean control above used the default 16 processes, identical to the buggy run.
- A NUMPROCESSES=1 re-run of the buggy pod hit a transient schema-metadata KeyError because DROP+recreate raced with COPY worker metadata refresh — also not the bug. Discarded that run. The authoritative buggy evidence is the FIRST buggy run (default processes, fresh keyspace), shown above.

## A/B contrast (one line)
- 3.11.9 (buggy): COPY FROM -> ParseError "Empty values are not allowed,  given up without retries"; table left EMPTY (0 rows). DATA LOST.
- 3.11.10 (fixed): COPY FROM -> success; row with empty-string list element preserved (1 row).

Disposition: **reproduced** (verbatim signature captured; A/B control confirms fix).
