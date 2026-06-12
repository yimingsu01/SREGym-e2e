# CASSANDRA-17467 — Reproduction Evidence Log

## Summary
**Timestamp issue with Cassandra 4.0.3 with Timezone value** (component: CQL/Syntax)

A timestamp literal that contains a **space before the timezone offset** (e.g.
`'2022-03-20 12:48:56 +0530'`) is rejected by the CQL timestamp parser in Cassandra 4.0.3 with
`InvalidRequest code=2200 [Invalid query] message="Unable to parse a date/time from ..."`.
The same literal **without** the space (`'2022-03-20 12:48:56+0530'`) is accepted. The bug is a
regression in the 4.0 line (the body states 3.11.x accepts both forms) and is fixed in 4.0.4.

- fixVersions: 4.0.4, 4.1-alpha1, 4.1
- Buggy image: `cassandra:4.0.3`
- Fixed control image: `cassandra:4.0.4` (fixVersion, and 4 <= 4.0-line ceiling 20 — valid A/B)

## Disposition: reproduced
Verbatim buggy signature captured on 4.0.3; identical workload succeeds on the 4.0.4 control.

## Tag correction
None. Classifier hint (topology=1node, confidence=H, trigger = "INSERT/SELECT with timestamp literal
'2022-03-20 12:48:56 +0530' (space before tz) -> InvalidRequest 'Unable to parse a date/time'")
matches the Jira body exactly. Pure single-node CQL parsing bug; no ring required.

## Topology
Single node. Two single-node pods in namespace `repro-17467`:
- `cass`        = cassandra:4.0.3 (buggy)
- `cass-fixed`  = cassandra:4.0.4 (fixed control)
Both confirmed Ready and serving CQL; release_version verified 4.0.3 and 4.0.4 respectively.

## Exact reproducer (from Jira body)
```sql
CREATE TABLE timetest ( id int PRIMARY KEY, enddate timestamp, startdate timestamp );
-- FAILS on 4.0.3, works on 3.11 / 4.0.4:
INSERT INTO timetest (id,startdate,enddate)
  VALUES (1,'2022-03-20 12:48:56 +0530','2022-03-20 12:48:56 +0530');
SELECT * FROM timetest WHERE id = 1 AND enddate = '2022-03-20 12:48:56 +0530';
-- Removing the space before the tz makes it work on 4.x: '2022-03-20 12:48:56+0530'
```
Quoting note: all CQL was passed via direct argv `kubectl exec -n repro-17467 <pod> -- cqlsh -e "<stmt>"`
(NOT wrapped in an extra `sh -c`), so the space inside the literal survives intact to the server.

---

## RAW OUTPUTS

### Version confirmation
```
$ kubectl exec -n repro-17467 cass       -- cqlsh -e "SELECT release_version FROM system.local"
 release_version
-----------------
           4.0.3

$ kubectl exec -n repro-17467 cass-fixed -- cqlsh -e "SELECT release_version FROM system.local"
 release_version
-----------------
           4.0.4
```

### Schema setup (buggy node `cass`, 4.0.3) — succeeded
```
$ kubectl exec -n repro-17467 cass -- cqlsh --request-timeout=60 -e \
    "CREATE KEYSPACE IF NOT EXISTS repro17467 WITH replication = {'class':'SimpleStrategy','replication_factor':1};"
ks exit=0
$ kubectl exec -n repro-17467 cass -- cqlsh --request-timeout=60 -e \
    "CREATE TABLE IF NOT EXISTS repro17467.timetest (id int PRIMARY KEY, enddate timestamp, startdate timestamp);"
tbl exit=0
-- DESCRIBE TABLE confirmed: id int PRIMARY KEY, enddate timestamp, startdate timestamp
```

### BUGGY NODE (cass, 4.0.3) — the bug

**TEST 1 — INSERT with SPACE before tz `'2022-03-20 12:48:56 +0530'` -> FAILS (the bug):**
```
$ kubectl exec -n repro-17467 cass -- cqlsh -e \
  "INSERT INTO repro17467.timetest (id,startdate,enddate) VALUES (1,'2022-03-20 12:48:56 +0530','2022-03-20 12:48:56 +0530')"
<stdin>:1:InvalidRequest: Error from server: code=2200 [Invalid query] message="Unable to parse a date/time from '2022-03-20 12:48:56 +0530'"
command terminated with exit code 2
```

**TEST 2 — INSERT with NO space `'2022-03-20 12:48:56+0530'` -> SUCCEEDS (within-version contrast):**
```
$ kubectl exec -n repro-17467 cass -- cqlsh -e \
  "INSERT INTO repro17467.timetest (id,startdate,enddate) VALUES (2,'2022-03-20 12:48:56+0530','2022-03-20 12:48:56+0530')"
exit=0
```

**TEST 3 — SELECT with SPACE before tz -> FAILS (same parse error):**
```
$ kubectl exec -n repro-17467 cass -- cqlsh -e \
  "SELECT * FROM repro17467.timetest WHERE id = 1 AND enddate = '2022-03-20 12:48:56 +0530' ALLOW FILTERING"
<stdin>:1:InvalidRequest: Error from server: code=2200 [Invalid query] message="Unable to parse a date/time from '2022-03-20 12:48:56 +0530'"
command terminated with exit code 2
```

**State on buggy node: row 1 (space INSERT) absent, row 2 (no-space INSERT) present:**
```
$ kubectl exec -n repro-17467 cass -- cqlsh -e "SELECT * FROM repro17467.timetest"
 id | enddate                         | startdate
----+---------------------------------+---------------------------------
  2 | 2022-03-20 07:18:56.000000+0000 | 2022-03-20 07:18:56.000000+0000
(1 rows)
```
(Note: +0530 offset correctly converted: 12:48:56 IST == 07:18:56 UTC for the accepted row.)

### FIXED CONTROL NODE (cass-fixed, 4.0.4) — A/B with IDENTICAL workload

```
$ kubectl exec -n repro-17467 cass-fixed -- cqlsh --request-timeout=60 -e "CREATE KEYSPACE IF NOT EXISTS repro17467 ..."  ks exit=0
$ kubectl exec -n repro-17467 cass-fixed -- cqlsh --request-timeout=60 -e "CREATE TABLE IF NOT EXISTS repro17467.timetest ..."  tbl exit=0
```

**Identical INSERT with SPACE before tz -> SUCCEEDS on fixed:**
```
$ kubectl exec -n repro-17467 cass-fixed -- cqlsh -e \
  "INSERT INTO repro17467.timetest (id,startdate,enddate) VALUES (1,'2022-03-20 12:48:56 +0530','2022-03-20 12:48:56 +0530')"
exit=0
```

**Identical SELECT with SPACE before tz -> SUCCEEDS on fixed, returns the row:**
```
$ kubectl exec -n repro-17467 cass-fixed -- cqlsh -e \
  "SELECT * FROM repro17467.timetest WHERE id = 1 AND enddate = '2022-03-20 12:48:56 +0530' ALLOW FILTERING"
 id | enddate                         | startdate
----+---------------------------------+---------------------------------
  1 | 2022-03-20 07:18:56.000000+0000 | 2022-03-20 07:18:56.000000+0000
(1 rows)
```

**State on fixed node: row 1 (space INSERT) PRESENT (vs. absent on buggy):**
```
$ kubectl exec -n repro-17467 cass-fixed -- cqlsh -e "SELECT * FROM repro17467.timetest"
 id | enddate                         | startdate
----+---------------------------------+---------------------------------
  1 | 2022-03-20 07:18:56.000000+0000 | 2022-03-20 07:18:56.000000+0000
(1 rows)
```

---

## Conclusion
Both controls confirm a genuine, client-visible version regression:
- **Within-version (4.0.3):** the literal differs only by one space; with-space FAILS, no-space SUCCEEDS.
  This isolates the trigger to the space before the tz offset, ruling out a malformed-literal/setup artifact.
- **Cross-version A/B:** the exact same with-space INSERT+SELECT that throws on 4.0.3 succeeds on 4.0.4.

Verbatim buggy signature:
`InvalidRequest: Error from server: code=2200 [Invalid query] message="Unable to parse a date/time from '2022-03-20 12:48:56 +0530'"`

Disposition: **reproduced**.

## Namespaces created (for teardown)
- repro-17467  (pods: cass [4.0.3], cass-fixed [4.0.4])
