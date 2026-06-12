# CASSANDRA-19401 — Reproduction Evidence Log

## Bug summary (from Jira ground truth)
**Title:** Nodetool import expects directory structure
**Components:** Local/SSTable
**fixVersions:** 4.0.13, 4.1.5, 5.0-rc1, 6.0-alpha1, 6.0
**Buggy version under test:** cassandra:4.1.4 (Jira reported on 4.1.3, Java 11/Linux)
**Fixed-image A/B control:** cassandra:4.1.5 (within ceiling 4.1->11)

The Cassandra 4.1 docs state that `nodetool import` does NOT require SSTables to be in a
specific `$KEYSPACE/$TABLE` directory path, because keyspace and table are given on the
command line. In reality, on 4.1.x, when the source directory is a FLAT directory whose
parent dir names do NOT match `<keyspace>/<table>`, `nodetool import` silently imports
nothing and logs `No new SSTables were found` (SSTableImporter.java:173) — exiting 0 with
no stdout. Moving the same SSTables into a `.../<keyspace>/<table>/` directory makes the
import succeed.

## Topology / tag verification
- topology = **1node** (single Cassandra pod). CONFIRMED — this is a purely local SSTable
  operation; no ring needed.
- confidence = H. trigger hint = "nodetool import --copy-data ks tbl /flat_path -> 'No new
  SSTables were found' instead of importing". CONFIRMED — matches body exactly.
- **tag_correction = none.** The classifier hint matched the Jira body in every respect.

## Environment
- kind cluster, context kind-kind, namespace **repro-19401** (created by this session).
- Keyspace **repro19401ks**, table **t** (unique to this session).
- Buggy pod: `cass` (cassandra:4.1.4). Fixed-control pod: `cass-fixed` (cassandra:4.1.5),
  same namespace.

## Version confirmation
```
$ kubectl exec -n repro-19401 cass -- cqlsh -e "SELECT release_version FROM system.local"
 release_version
-----------------
           4.1.4
$ kubectl exec -n repro-19401 cass-fixed -- cqlsh -e "SELECT release_version FROM system.local"
           4.1.5
```

## Reproducer steps (buggy pod, cassandra:4.1.4)
1. Create keyspace `repro19401ks` (SimpleStrategy RF=1), table `t (id int PK, v text)`,
   insert 5 rows, `nodetool flush repro19401ks t`. (count = 5; SSTables `nb-1-big-*` written
   under `/var/lib/cassandra/data/repro19401ks/t-<uuid>/`.)
2. Stage the FULL component set into two locations:
   - FLAT path `/tmp/staging`               (no ks/tbl subdirs)  <- triggers bug
   - NESTED path `/tmp/nested/repro19401ks/t` (ks/tbl subdirs)   <- within-version control
   `cp $(find /var/lib/cassandra/data/repro19401ks/t-*/ -maxdepth 1 -type f) <dst>/`
3. `chown -R 999:999 <dst>` + `chmod -R u+rwX <dst>` so the cassandra server uid (999) can
   write to the dir. (Without this the importer fails earlier with
   `Insufficient permissions on directory` at SSTableImporter.java:242 — a separate guard,
   NOT the bug. The kubectl-exec user is root, but the daemon runs as uid 999.)
4. `TRUNCATE repro19401ks.t` (count = 0) so the import result is client-visible.
5. Run `nodetool import --copy-data repro19401ks t <path>` for each path.

NOTE on nodetool behavior: on the FLAT path, `nodetool import` produced **NO stdout and
exited 0**. The "No new SSTables were found" signal is a server-side INFO log line, not a
nodetool error. Success/failure was judged by `SELECT count(*)` and the server log.

---

## BUGGY RESULT — FLAT PATH /tmp/staging (cassandra:4.1.4)

Command:
```
$ kubectl exec -n repro-19401 cass -- nodetool import --copy-data repro19401ks t /tmp/staging
(no output, exit 0)
$ kubectl exec -n repro-19401 cass -- cqlsh -e "SELECT count(*) FROM repro19401ks.t"
 count
-------
     0
```

Server log (VERBATIM buggy signature — matches Jira body exactly):
```
INFO  [RMI TCP Connection(6)-127.0.0.1] 2026-06-12 03:00:08,246 SSTableImporter.java:72 - Loading new SSTables for repro19401ks/t: Options{srcPaths='[/tmp/staging]', resetLevel=true, clearRepaired=true, verifySSTables=true, verifyTokens=true, invalidateCaches=true, extendedVerify=false, copyData= true}
INFO  [RMI TCP Connection(6)-127.0.0.1] 2026-06-12 03:00:08,251 SSTableImporter.java:173 - No new SSTables were found for repro19401ks/t
```
=> Nothing imported. Table still empty (count 0). This is the bug.

---

## WITHIN-VERSION CONTROL — NESTED PATH /tmp/nested/repro19401ks/t (cassandra:4.1.4)

Identical SSTables, only the directory naming differs (parent dirs = keyspace/table).
```
$ kubectl exec -n repro-19401 cass -- nodetool import --copy-data repro19401ks t /tmp/nested/repro19401ks/t
(no output, exit 0)
$ kubectl exec -n repro-19401 cass -- cqlsh -e "SELECT count(*) FROM repro19401ks.t"
 count
-------
     5
```
Server log:
```
INFO  [RMI TCP Connection(8)-127.0.0.1] 2026-06-12 03:00:19,344 SSTableImporter.java:72 - Loading new SSTables for repro19401ks/t: Options{srcPaths='[/tmp/nested/repro19401ks/t]', ...}
INFO  [RMI TCP Connection(8)-127.0.0.1] 2026-06-12 03:00:19,380 SSTableImporter.java:177 - Loading new SSTables and building secondary indexes for repro19401ks/t: [BigTableReader(path='/var/lib/cassandra/data/repro19401ks/t-b2365f90660a11f1aae7079bdc538b32/nb-2-big-Data.db')]
INFO  [RMI TCP Connection(8)-127.0.0.1] 2026-06-12 03:00:19,380 SSTableImporter.java:190 - Done loading load new SSTables for repro19401ks/t
```
=> Same SSTables import fine when the dir is named `<keyspace>/<table>`. Proves the SSTables
are valid; the only difference is directory naming. Isolates the failure to import path
handling (the discriminator is directory NAMING, not flat vs nested per se).

---

## FIXED-IMAGE A/B CONTROL — FLAT PATH /tmp/staging (cassandra:4.1.5)

Identical workload (create/insert/flush, stage to FLAT /tmp/staging, chown 999, truncate),
then flat-path import on the FIXED image:
```
$ kubectl exec -n repro-19401 cass-fixed -- nodetool import --copy-data repro19401ks t /tmp/staging
(no output, exit 0)
$ kubectl exec -n repro-19401 cass-fixed -- cqlsh -e "SELECT count(*) FROM repro19401ks.t"
 count
-------
     5
```
Server log (4.1.5):
```
INFO  [RMI TCP Connection(4)-127.0.0.1] 2026-06-12 03:02:17,768 SSTableImporter.java:72 - Loading new SSTables for repro19401ks/t: Options{srcPaths='[/tmp/staging]', ...}
INFO  [RMI TCP Connection(4)-127.0.0.1] 2026-06-12 03:02:17,825 SSTableImporter.java:177 - Loading new SSTables and building secondary indexes for repro19401ks/t: [BigTableReader(path='/var/lib/cassandra/data/repro19401ks/t-1a5afcc0660b11f183ac6dfa66798868/nb-2-big-Data.db')]
INFO  [RMI TCP Connection(4)-127.0.0.1] 2026-06-12 03:02:17,825 SSTableImporter.java:190 - Done loading load new SSTables for repro19401ks/t
```
=> On 4.1.5 the SAME flat-path import that FAILED on 4.1.4 now SUCCEEDS (count 5,
"Done loading"). Confirms the fix and pins the bug to 4.1.4.

---

## A/B summary table
| Pod        | Version | Source path                       | Log result                         | count |
|------------|---------|-----------------------------------|------------------------------------|-------|
| cass       | 4.1.4   | /tmp/staging (flat)               | **No new SSTables were found**     | 0     |
| cass       | 4.1.4   | /tmp/nested/repro19401ks/t        | Done loading                       | 5     |
| cass-fixed | 4.1.5   | /tmp/staging (flat)               | Done loading                       | 5     |

## Disposition: REPRODUCED
Verbatim buggy signature:
`SSTableImporter.java:173 - No new SSTables were found for repro19401ks/t`
(flat-path `nodetool import --copy-data` on cassandra:4.1.4; identical operation succeeds on
4.1.5 and on a `<ks>/<tbl>`-named dir on 4.1.4). Matches the Jira description verbatim.

## Tooling findings
None affecting SREGym tooling. Operational note only: `nodetool import --copy-data` requires
the source directory to be writable by the cassandra server uid (999); since `kubectl exec`
runs as root, a staging dir must be chown'd to 999 or the importer fails earlier with
`Insufficient permissions on directory` (a different guard at SSTableImporter.java:242, not
this bug). Any automated reproducer for this issue must account for that uid mismatch.

## Teardown
`kubectl delete ns repro-19401 --wait=false` (executed at end of session).
