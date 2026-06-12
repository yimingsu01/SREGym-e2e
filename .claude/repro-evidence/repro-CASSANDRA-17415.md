# CASSANDRA-17415 — Reproduction Evidence

## Bug (primary source: /tmp/jira_repro/CASSANDRA-17415.json)
- **Summary**: "dropping of a materialized view does not create a snapshot with dropped- prefix"
- **Description**: When `auto_snapshot: true` and an MV is dropped, the snapshot directory name
  does NOT start with the `dropped-` prefix as a normal table would. "This is an issue for 3.11.x
  only. In 4.x, the code was refactored a lot and it does not happen there."
- **fixVersions**: 3.11.13   **components**: Feature/Materialized Views
- **Buggy version**: cassandra:3.11.12
- **Control (fixed)**: cassandra:3.11.19 (any >= 3.11.13 has the fix; 19 <= 3.11 ceiling).
  cassandra:3.11.13 was unavailable (Docker Hub 429 unauthenticated pull-rate limit), so the
  cached 3.11.19 image on kind-worker was used as the fixed-version A/B control.

## Tag correction vs classifier HINT
- Hint: topology=1node, trigger "auto_snapshot:true + DROP MATERIALIZED VIEW -> snapshot dir name
  missing 'dropped-' prefix". This matches the Jira body exactly. **No correction needed.**

## Why the verbatim signature is a filesystem line (not a server exception)
This bug produces NO cqlsh/server error. The DROP succeeds; the observable is the snapshot
directory NAME on disk. It is operator-visible: snapshot-listing/recovery tooling keys on the
`dropped-` prefix to identify snapshots taken at drop time. The signature is made legible as a
**within-image contrast** on the SAME 3.11.12 binary: drop a normal table AND an MV, then show the
table's snapshot has `dropped-` and the MV's does not. The 3.11.19 control then confirms the fix.

## Topology
Single Cassandra pod per version in namespace `repro-17415` (this is a local SSTable/snapshot
directory-naming issue; no ring needed). Keyspace: `repro17415_ks`.
- cass-1112  -> cassandra:3.11.12 (BUGGY)
- cass-11119 -> cassandra:3.11.19 (FIXED control)
auto_snapshot confirmed `true` in /etc/cassandra/cassandra.yaml on BOTH pods (stock default; no
config-append needed).

## Reproducer (identical on both pods)
```
CREATE KEYSPACE repro17415_ks WITH replication={'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE repro17415_ks.base (id int PRIMARY KEY, val text);
CREATE MATERIALIZED VIEW repro17415_ks.mv AS
  SELECT id,val FROM repro17415_ks.base WHERE id IS NOT NULL AND val IS NOT NULL
  PRIMARY KEY (val, id);
INSERT INTO repro17415_ks.base (id,val) VALUES (1,'a'),(2,'b'),(3,'c');   -- writes flow base->MV
nodetool flush repro17415_ks       -- (ran with JVM_OPTS="-Dcom.sun.jndi.rmiURLParsing=legacy"; see tooling note)
# confirm SSTables for BOTH base-* and mv-* exist on disk
DROP MATERIALIZED VIEW repro17415_ks.mv;     -- auto_snapshot fires
DROP TABLE repro17415_ks.base;               -- auto_snapshot fires
# observable:
find /var/lib/cassandra/data/repro17415_ks -type d -path '*/snapshots/*'
```

## RAW OUTPUT — BUGGY 3.11.12 (cass-1112)

auto_snapshot:
```
$ kubectl exec -n repro-17415 cass-1112 -- grep auto_snapshot /etc/cassandra/cassandra.yaml
auto_snapshot: true
```

SSTables on disk before drop (both present):
```
/var/lib/cassandra/data/repro17415_ks/base-dd6538d0661911f1a8c4edaf56a013df/me-1-big-Data.db
/var/lib/cassandra/data/repro17415_ks/mv-e1917d60661911f1a8c4edaf56a013df/me-1-big-Data.db
```

Schema after both DROPs (empty -> both dropped):
```
(no tables/views left in keyspace -> both dropped)
```

**PRIMARY OBSERVABLE — snapshot directories on disk (BUGGY):**
```
$ kubectl exec -n repro-17415 cass-1112 -- sh -c "find /var/lib/cassandra/data/repro17415_ks -type d -path '*/snapshots/*'"
/var/lib/cassandra/data/repro17415_ks/base-dd6538d0661911f1a8c4edaf56a013df/snapshots/dropped-1781239759581-base
/var/lib/cassandra/data/repro17415_ks/mv-e1917d60661911f1a8c4edaf56a013df/snapshots/1781239738805-mv
```
==> base table snapshot = `dropped-1781239759581-base`  (HAS `dropped-` prefix — correct)
==> MV         snapshot = `1781239738805-mv`            (MISSING `dropped-` prefix — THE BUG)

## RAW OUTPUT — FIXED 3.11.19 control (cass-11119), identical reproducer

auto_snapshot:
```
$ kubectl exec -n repro-17415 cass-11119 -- grep "^auto_snapshot" /etc/cassandra/cassandra.yaml
auto_snapshot: true
```

SSTables on disk before drop (both present):
```
/var/lib/cassandra/data/repro17415_ks/base-26785840661a11f1a81d9b74738582e0/me-1-big-Data.db
/var/lib/cassandra/data/repro17415_ks/mv-2838a5e0661a11f1a81d9b74738582e0/me-1-big-Data.db
```

**CONTROL OBSERVABLE — snapshot directories on disk (FIXED):**
```
$ kubectl exec -n repro-17415 cass-11119 -- sh -c "find /var/lib/cassandra/data/repro17415_ks -type d -path '*/snapshots/*' | sort"
/var/lib/cassandra/data/repro17415_ks/base-26785840661a11f1a81d9b74738582e0/snapshots/dropped-1781239887812-base
/var/lib/cassandra/data/repro17415_ks/mv-2838a5e0661a11f1a81d9b74738582e0/snapshots/dropped-1781239845416-mv
```
==> base table snapshot = `dropped-1781239887812-base`  (HAS `dropped-` prefix)
==> MV         snapshot = `dropped-1781239845416-mv`     (HAS `dropped-` prefix — FIXED)

## A/B comparison (the decisive result)

| Version          | base-table snapshot dir              | MV snapshot dir                        | MV has `dropped-`? |
|------------------|--------------------------------------|----------------------------------------|--------------------|
| 3.11.12 (buggy)  | `dropped-1781239759581-base`         | `1781239738805-mv`                     | NO  (BUG)          |
| 3.11.19 (fixed)  | `dropped-1781239887812-base`         | `dropped-1781239845416-mv`             | YES (fixed)        |

Both snapshots are physically created on both versions (auto_snapshot works); the ONLY difference
is the missing `dropped-` prefix on the MV snapshot under 3.11.12. This matches CASSANDRA-17415
exactly, is 3.11.x-only as the body states, and is corrected in the 3.11.13+ line (verified on
3.11.19).

## Bonus: nodetool listsnapshots (expected null on both, per advisor)
```
--- BUGGY 3.11.12 ---:  Snapshot Details:  There are no snapshots
--- FIXED 3.11.19 ---:  Snapshot Details:  There are no snapshots
```
listsnapshots does NOT enumerate snapshots of already-dropped tables (table gone from schema);
the dropped table's directory persists on disk precisely because it holds the snapshot. Direct
filesystem `find` is the reliable observable, and it shows the contrast.

## VERBATIM SIGNATURE (literal filesystem line, buggy 3.11.12)
/var/lib/cassandra/data/repro17415_ks/mv-e1917d60661911f1a8c4edaf56a013df/snapshots/1781239738805-mv

(MV snapshot directory lacks the `dropped-` prefix; the sibling normal-table snapshot on the same
node is `dropped-1781239759581-base`.)

## Tooling findings
- cassandra:3.11.13 could not be pulled: Docker Hub returned HTTP 429
  "toomanyrequests: You have reached your unauthenticated pull rate limit". Used the already-cached
  cassandra:3.11.19 (also fixed, <= 3.11 ceiling 19) as the A/B control, pinned to kind-worker with
  imagePullPolicy: IfNotPresent. SREGym runs that need specific 3.11.13/etc. images on a fresh node
  would benefit from authenticated Docker Hub pulls or a local registry mirror.
- `nodetool` in the cassandra:3.11.x images fails JMX connect with
  "URISyntaxException: 'Malformed IPv6 address at index 7: rmi://[127.0.0.1]:7199'" on the bundled
  JDK. Worked around by exporting `JVM_OPTS="-Dcom.sun.jndi.rmiURLParsing=legacy"` for nodetool
  invocations. This is a generic image/JDK issue, not specific to this bug. (RECORD ONLY — not fixed.)
- DROP MATERIALIZED VIEW / DROP TABLE returned client-side `OperationTimedOut` from cqlsh
  (schema-agreement wait) but executed server-side; verified by schema descriptions and the
  on-disk snapshot directories.

## Disposition: reproduced
Verbatim buggy signature captured (filesystem line above) + decisive A/B fixed control.
