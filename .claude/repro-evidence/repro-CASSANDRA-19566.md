# CASSANDRA-19566 — JSON encoded timestamp value does not always match non-JSON encoded value

- **Buggy version:** cassandra:4.1.4
- **Fixed-control version:** cassandra:4.1.5 (4.1.4 patch+1 = 5, <= 4.1 ceiling 11; 4.1.5 is in fixVersions)
- **Topology:** 1 node (single pod). Matches classifier hint `1node`.
- **Namespace:** repro-19566   **Keyspace:** repro19566
- **fixVersions (Jira):** 4.0.13, 4.1.5, 5.0-rc1, 6.0-alpha1, 6.0
- **Components:** Legacy/Core, Legacy/CQL
- **Disposition:** REPRODUCED

## Reproducer (from Jira body, verbatim mechanism)
1. Single-node Cassandra 4.1.4.
2. `CREATE TABLE tbl (id int, ts timestamp, primary key (id));`
3. `INSERT INTO tbl (id, ts) VALUES (1, -13767019200000);`  (a pre-1582 / pre-Gregorian-cutover timestamp)
4. `SELECT tounixtimestamp(ts), ts, tojson(ts) FROM tbl WHERE id=1;`
5. `SELECT JSON * FROM tbl WHERE id=1;`

The discriminator is **internal row consistency**: the same stored long must render to the same calendar
date whether output as a bare timestamp (`ts`), via `tojson(ts)`, or via `SELECT JSON *`. On the buggy
version they disagree by exactly 10 days (Julian vs proleptic-Gregorian calendar offset for year 1533).

## Deployment
Two pods deployed in parallel in namespace `repro-19566`:
- `cass-buggy`  -> cassandra:4.1.4
- `cass-fixed`  -> cassandra:4.1.5
Both reached `condition=Ready` and answered `SELECT now() FROM system.local`.

---

## BUGGY — cassandra:4.1.4  (cass-buggy)

Setup:
```
kubectl exec -n repro-19566 cass-buggy -- cqlsh --request-timeout=30 -e \
  "CREATE KEYSPACE IF NOT EXISTS repro19566 WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};"
kubectl exec -n repro-19566 cass-buggy -- cqlsh --request-timeout=30 -e \
  "CREATE TABLE IF NOT EXISTS repro19566.tbl (id int, ts timestamp, primary key (id));
   INSERT INTO repro19566.tbl (id, ts) VALUES (1, -13767019200000);"
```

Query 1:
```
$ kubectl exec -n repro-19566 cass-buggy -- cqlsh -e \
    "SELECT tounixtimestamp(ts), ts, tojson(ts) FROM repro19566.tbl WHERE id=1;"

 system.tounixtimestamp(ts) | ts                              | system.tojson(ts)
----------------------------+---------------------------------+----------------------------
            -13767019200000 | 1533-09-28 12:00:00.000000+0000 | "1533-09-18 12:00:00.000Z"

(1 rows)
```

Query 2:
```
$ kubectl exec -n repro-19566 cass-buggy -- cqlsh -e "SELECT JSON * FROM repro19566.tbl WHERE id=1;"

 [json]
---------------------------------------------
 {"id": 1, "ts": "1533-09-18 12:00:00.000Z"}

(1 rows)
```

### BUGGY SIGNATURE (verbatim)
Same stored value `-13767019200000` renders as TWO different dates in one row:
```
 -13767019200000 | 1533-09-28 12:00:00.000000+0000 | "1533-09-18 12:00:00.000Z"
```
- `ts`            -> `1533-09-28` (proleptic Gregorian, correct)
- `tojson(ts)`    -> `1533-09-18` (WRONG, 10 days off)
- `SELECT JSON *` -> `1533-09-18` (WRONG, second witness, same code path)

The `tounixtimestamp(ts)` column (`-13767019200000`) proves both renderings derive from the identical
stored long; the divergence is purely in the JSON serialization path's date formatting.

---

## CONTROL — cassandra:4.1.5  (cass-fixed), IDENTICAL workload

```
$ kubectl exec -n repro-19566 cass-fixed -- cqlsh -e "SELECT release_version FROM system.local;"
 release_version
-----------------
           4.1.5

$ kubectl exec -n repro-19566 cass-fixed -- cqlsh -e \
    "SELECT tounixtimestamp(ts), ts, tojson(ts) FROM repro19566.tbl WHERE id=1;"

 system.tounixtimestamp(ts) | ts                              | system.tojson(ts)
----------------------------+---------------------------------+----------------------------
            -13767019200000 | 1533-09-28 12:00:00.000000+0000 | "1533-09-28 12:00:00.000Z"

(1 rows)

$ kubectl exec -n repro-19566 cass-fixed -- cqlsh -e "SELECT JSON * FROM repro19566.tbl WHERE id=1;"

 [json]
---------------------------------------------
 {"id": 1, "ts": "1533-09-28 12:00:00.000Z"}

(1 rows)
```

On the fixed version all three renderings AGREE on `1533-09-28`. The bug is gone. This isolates the
defect to 4.1.4's JSON timestamp serialization (fixed in 4.1.5), not to the test workload or environment.

---

## Conclusion
REPRODUCED with a verbatim buggy signature. On 4.1.4 a single stored timestamp value renders as
`1533-09-28` (bare `ts`) but `1533-09-18` (`tojson(ts)` and `SELECT JSON *`) — a client-visible 10-day
discrepancy in one row. The fixed image 4.1.5 produces agreement (`1533-09-28` everywhere) under the
identical workload, confirming the A/B control.

Tag correction: none — classifier hint (topology=1node, the INSERT -13767019200000 + ts vs toJson/JSON
date-mismatch trigger) matches the Jira body exactly.
