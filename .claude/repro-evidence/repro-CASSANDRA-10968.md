# CASSANDRA-10968 — Reproduction Evidence Log

## Bug
**Summary:** When taking snapshot, manifest.json contains incorrect or no files when column
family has secondary indexes.
**Component:** Feature/2i Index
**Buggy version:** 3.11.6
**Fix versions:** 2.1.x, 2.2.17, 3.0.21, **3.11.7**, 4.0-beta1, 4.0
**Disposition:** REPRODUCED (deterministic, with verbatim buggy signature + A/B control)

## Classifier hint vs reality (tag_correction)
- Hint: topology=1node, confidence=M, trigger="table with secondary index + nodetool snapshot ->
  manifest.json missing/incorrect file list".
- Reality: hint is CORRECT. 1-node single pod is sufficient. The body's "sometimes none, sometimes
  some files" is not a race — it is deterministic per flush/compaction state. The manifest is written
  once per CFS in a loop (base table + each index CFS) all targeting the BASE table's manifest.json
  path, so each later iteration OVERWRITES it; the final content is the LAST index CFS's bare file
  list (no path prefix) and OMITS the base table's actual data files. No tag correction needed.

## Reproducer (exact)
1. Single-node Cassandra 3.11.6 pod in kind (ns repro-10968).
2. Create keyspace + table + secondary index:
   - CREATE KEYSPACE repro10968 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
   - CREATE TABLE repro10968.users (id int PRIMARY KEY, email text, city text);
   - CREATE INDEX users_city_idx ON repro10968.users (city);
3. 3x (INSERT 2 rows; `nodetool flush repro10968 users`)  -> base + index each have md-1,md-2,md-3.
4. `nodetool compact repro10968 users`  -> compacts ONLY the base table to a single sstable md-4,
   while the index keeps md-1,md-2,md-3. (This forces an UNAMBIGUOUS generation mismatch between base
   and index. Without it, base and index share md-1/2/3 and the buggy manifest passes a by-name glance
   — see snap1 below.)
5. `nodetool snapshot -t snap2 repro10968`.
6. Inspect the BASE table's snapshots/snap2/manifest.json and compare to the physical *-Data.db files.

## Environment
- kind-kind, 4 nodes. Buggy pod `cass` (cassandra:3.11.6) on kind-worker.
- Control pod `cass-fixed` (cassandra:3.11.9, cached on kind-worker3) — post-fix (fix=3.11.7).
  NOTE: cassandra:3.11.7 (canonical A/B) could NOT be pulled — Docker Hub returned
  "429 Too Many Requests ... unauthenticated pull rate limit". Used cached post-fix 3.11.9 instead,
  which is a valid fixed-version control (fix landed in 3.11.7 << 3.11.9).

================================================================================
## BUGGY 3.11.6 — RAW OUTPUT
================================================================================

$ kubectl exec -n repro-10968 cass -- cqlsh -e "SELECT release_version FROM system.local"
 release_version
-----------------
          3.11.6

# After 3x insert+flush, base table users-31f2ff60660e11f1a29fd9bb1953835c had:
#   md-1-big-Data.db  md-2-big-Data.db  md-3-big-Data.db
# index .users_city_idx had: md-1,md-2,md-3 (SAME generation numbers as base -> ambiguous)

# --- snap1 (taken BEFORE compaction; base & index BOTH md-1/2/3) ---
$ cat .../users-31f2.../snapshots/snap1/manifest.json
{"files":["md-2-big-Data.db","md-1-big-Data.db","md-3-big-Data.db"]}
# physical base Data.db in snap1: md-1,md-2,md-3  -> LOOKS correct by filename only, because index
#   shares the same generations. This is the "trap" that hides the bug. Note: NO path prefix and NO
#   index manifest, already suspicious.

# --- Compact base only ---
$ kubectl exec -n repro-10968 cass -- nodetool compact repro10968 users
# base live Data.db now: md-4-big-Data.db   (single compacted sstable)
# index live Data.db still: md-1,md-2,md-3

# --- snap2 (base=md-4, index=md-1/2/3) -> UNAMBIGUOUS ---
$ kubectl exec -n repro-10968 cass -- nodetool snapshot -t snap2 repro10968
Requested creating snapshot(s) for [repro10968] with snapshot name [snap2] and options {skipFlush=false}
Snapshot directory: snap2

$ cat .../users-31f2.../snapshots/snap2/manifest.json
{"files":["md-2-big-Data.db","md-1-big-Data.db","md-3-big-Data.db"]}      <<< BUGGY SIGNATURE

$ find .../snapshots/snap2 -maxdepth 1 -name '*-Data.db'
.../snapshots/snap2/md-4-big-Data.db                  <<< ONLY base file physically present
$ find .../snapshots/snap2/.users_city_idx -maxdepth 1 -name '*-Data.db'
.../snapshots/snap2/.users_city_idx/md-1-big-Data.db
.../snapshots/snap2/.users_city_idx/md-2-big-Data.db
.../snapshots/snap2/.users_city_idx/md-3-big-Data.db
$ ls .../snapshots/snap2/.users_city_idx/manifest.json
No such file or directory   (index has no own manifest)

### WHY THIS IS WRONG
The BASE manifest.json lists ["md-1/2/3-big-Data.db"] — these are the INDEX's sstable generations.
But the base snapshot directory physically contains ONLY md-4-big-Data.db. So the manifest:
  (a) references files (md-1/2/3) that DO NOT EXIST at the base path, and
  (b) OMITS the base table's actual data file (md-4).
A restore/backup tool that trusts manifest.json would fail to locate the listed files and would
miss the real base data. This matches the report ("manifest.json contains incorrect ... files").

================================================================================
## CONTROL — FIXED 3.11.9 — RAW OUTPUT (identical workload)
================================================================================

$ kubectl exec -n repro-10968 cass-fixed -- cqlsh -e "SELECT release_version FROM system.local"
 release_version
-----------------
          3.11.9

# base live Data.db after compact: md-4-big-Data.db
# index live Data.db: md-1,md-2,md-3   (same generation layout as the buggy run)

$ kubectl exec -n repro-10968 cass-fixed -- nodetool snapshot -t snap2 repro10968
Requested creating snapshot(s) for [repro10968] with snapshot name [snap2] and options {skipFlush=false}
Snapshot directory: snap2

$ cat .../users-4be0.../snapshots/snap2/manifest.json
{"files":["md-4-big-Data.db",".users_city_idx\/md-3-big-Data.db",".users_city_idx\/md-1-big-Data.db",".users_city_idx\/md-2-big-Data.db"]}

$ find .../snapshots/snap2 -maxdepth 1 -name '*-Data.db'
.../snapshots/snap2/md-4-big-Data.db
$ find .../snapshots/snap2/.users_city_idx -maxdepth 1 -name '*-Data.db'
.../snapshots/snap2/.users_city_idx/md-1-big-Data.db
.../snapshots/snap2/.users_city_idx/md-2-big-Data.db
.../snapshots/snap2/.users_city_idx/md-3-big-Data.db

### CONTROL RESULT: CORRECT
Fixed 3.11.9 manifest lists the base file md-4-big-Data.db AND all index files with proper
".users_city_idx/" relative path prefixes. The manifest exactly matches the physical contents
(base + index). No missing or incorrect files. Bug does NOT reproduce on the fixed image.

================================================================================
## A/B SUMMARY
================================================================================
| version         | base manifest.json "files"                                                  | matches physical? |
|-----------------|-----------------------------------------------------------------------------|-------------------|
| 3.11.6 (BUGGY)  | ["md-2-big-Data.db","md-1-big-Data.db","md-3-big-Data.db"]                   | NO — lists index gens md-1/2/3, omits base md-4, no path prefix |
| 3.11.9 (FIXED)  | ["md-4-big-Data.db",".users_city_idx/md-3..","..md-1..","..md-2-big-Data.db"]| YES — base md-4 + index files with .users_city_idx/ prefixes    |

Verbatim buggy signature (literal copy of the on-disk base manifest, 3.11.6 snap2):
{"files":["md-2-big-Data.db","md-1-big-Data.db","md-3-big-Data.db"]}
(physical base snapshot dir contained only md-4-big-Data.db; md-1/2/3 are the index's sstables.)

## Teardown
Deleted namespace repro-10968 (kubectl delete ns repro-10968 --wait=false).
No pre-existing namespace touched.
