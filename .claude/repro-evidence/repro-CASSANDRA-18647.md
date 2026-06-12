# CASSANDRA-18647 — CASTing a float to decimal adds wrong digits

- **Disposition:** reproduced (verbatim buggy signature + A/B control)
- **Buggy version:** cassandra:4.1.2
- **Control (fixed) version:** cassandra:4.1.3  (fixVersions lists 4.1.3; 4.1 ceiling is .11, so 4.1.3 image exists)
- **Topology:** 1 node (single pod). Pure CQL/Semantics bug, no ring needed. Hint topology=1node CORRECT.
- **Namespace:** repro-18647 (torn down at end). Keyspace: repro18647.
- **Components (Jira):** CQL/Semantics. fixVersions: 3.11.16, 4.0.11, 4.1.3, 5.0-alpha1, 5.0
- **tag_correction:** none — hint trigger (CAST float->decimal adds spurious digits via float->double->decimal path) and topology (1node) both match the Jira body exactly.

## Reproducer (extracted verbatim from Jira description)
Create a table with a `float` (32-bit) column `e`, insert `5.2`, then `SELECT CAST(e AS decimal)`.
The cast wrongly routes through `double` (64-bit) and picks up extra wrong digits, returning
`5.199999809265137` instead of `5.2`. Body contrasts this with `CAST(e AS text)` which is correct (`5.2`).

```
CREATE KEYSPACE repro18647 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE repro18647.tbl (p int PRIMARY KEY, e float);
INSERT INTO repro18647.tbl (p, e) VALUES (1, 5.2);
SELECT CAST(e AS decimal) FROM repro18647.tbl WHERE p=1;
```

## Environment
Existing kind cluster (context kind-kind, 4 nodes). Two pods deployed together in repro-18647:
`cass-412` (image cassandra:4.1.2, buggy) and `cass-413` (image cassandra:4.1.3, control).
Both reached Ready and cqlsh answered before the workload.
NOTE: initial multi-statement cqlsh `-e` DDL hit `OperationTimedOut` on the freshly-started node
(slow schema agreement). Re-running DDL as separate statements with `--request-timeout=60` succeeded.
This is a test-harness timing nuisance, not the bug.

---

## BUGGY — cassandra:4.1.2 (pod cass-412)  RAW OUTPUT

```
$ kubectl exec -n repro-18647 cass-412 -- cqlsh -e "SELECT release_version FROM system.local;"
 release_version
-----------------
           4.1.2
(1 rows)

$ kubectl exec -n repro-18647 cass-412 -- cqlsh -e "SELECT e FROM repro18647.tbl WHERE p=1;"
 e
-----
 5.2
(1 rows)

$ kubectl exec -n repro-18647 cass-412 -- cqlsh -e "SELECT CAST(e AS decimal) FROM repro18647.tbl WHERE p=1;"
 cast(e as decimal)
--------------------
  5.199999809265137      <==== BUG: spurious extra digits (exactly matches Jira body)
(1 rows)

$ kubectl exec -n repro-18647 cass-412 -- cqlsh -e "SELECT CAST(e AS text) FROM repro18647.tbl WHERE p=1;"
 cast(e as text)
-----------------
             5.2         <==== contrast path is CORRECT (matches Jira body)
(1 rows)

$ kubectl exec -n repro-18647 cass-412 -- cqlsh -e "SELECT CAST(e AS double) FROM repro18647.tbl WHERE p=1;"
 cast(e as double)
-------------------
               5.2       (cqlsh display-rounds the double; the decimal path exposes the full widened value)
(1 rows)
```

**Verbatim buggy signature:** `5.199999809265137`  (CAST(e AS decimal) on float 5.2)

---

## CONTROL — cassandra:4.1.3 (pod cass-413)  RAW OUTPUT — identical workload

```
$ kubectl exec -n repro-18647 cass-413 -- cqlsh -e "SELECT release_version FROM system.local;"
 release_version
-----------------
           4.1.3
(1 rows)

$ kubectl exec -n repro-18647 cass-413 -- cqlsh -e "SELECT CAST(e AS decimal) FROM repro18647.tbl WHERE p=1;"
 cast(e as decimal)
--------------------
                5.2      <==== FIXED: clean 5.2, no spurious digits
(1 rows)

$ kubectl exec -n repro-18647 cass-413 -- cqlsh -e "SELECT CAST(e AS text) FROM repro18647.tbl WHERE p=1;"
 cast(e as text)
-----------------
             5.2
(1 rows)
```

## Conclusion
Same value (`5.2`), same DDL, same query — the only difference is the Cassandra version.
- 4.1.2 (buggy): `CAST(e AS decimal)` -> `5.199999809265137`  (float widened to double then to decimal, carrying spurious digits)
- 4.1.3 (fixed):  `CAST(e AS decimal)` -> `5.2`
Client-visible wrong query result. Deterministic, single-node. **reproduced.**
