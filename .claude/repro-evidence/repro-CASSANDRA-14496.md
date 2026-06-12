# CASSANDRA-14496 — TWCS erroneously disabling tombstone compactions when unchecked_tombstone_compaction=true

- **Buggy version:** cassandra:4.0.0
- **Fixed control:** cassandra:4.0.20 (fix shipped in 4.0.1 per fixVersions; persists through 4.0 line; 4.0.1 image hit Docker Hub 429 rate-limit so used 4.0.20, which is preloaded on kind-worker and contains the fix; ceiling 4.0->20)
- **Namespace:** repro-14496 (created by me)
- **Keyspace:** repro14496_ks (unique)
- **Topology:** single pod per version (1node). Hint said 1node — CORRECT.
- **Disposition:** REPRODUCED
- **Component:** Local/Compaction
- **fixVersions:** 3.11.11, 4.0.1, 4.1-alpha1, 4.1

## Primary source (Jira body) — the real reproducer
From /tmp/jira_repro/CASSANDRA-14496.json. The buggy code in `TimeWindowCompactionStrategy.java`:
```java
this.options = new TimeWindowCompactionStrategyOptions(options);
if (!options.containsKey(AbstractCompactionStrategy.TOMBSTONE_COMPACTION_INTERVAL_OPTION)
    && !options.containsKey(AbstractCompactionStrategy.TOMBSTONE_THRESHOLD_OPTION))
{
    disableTombstoneCompactions = true;
    logger.debug("Disabling tombstone compactions for TWCS");
}
else
    logger.debug("Enabling tombstone compactions for TWCS");
```
The disabling branch only checks `tombstone_compaction_interval` and `tombstone_threshold`. It IGNORES
`unchecked_tombstone_compaction`. So a TWCS table with `unchecked_tombstone_compaction=true` but NEITHER
of the two other options set still has tombstone compactions DISABLED — the opposite of what
`unchecked_tombstone_compaction=true` is supposed to mean. The reporter's own workaround is to add
`tombstone_compaction_interval` (any value) which flips the branch to "Enabling".

The classifier hint ("TWCS table with unchecked_tombstone_compaction=true but no tombstone_threshold/interval
set -> tombstone compactions never run") MATCHES the body. tag_correction = none.

Note on observability: the BEHAVIORAL effect (an actual tombstone-purging compaction running) cannot be
forced in-budget — `worthDroppingTombstones()` gates on `tombstone_compaction_interval`, which defaults to
86400s (1 day) when unset, and setting it is exactly what flips the bug condition. The
client/operator-visible signature of this config-handling bug is the DEBUG log line emitted at TWCS
instantiation, which is the documented mechanism in the Jira body. This line is DEBUG-level under
org.apache.cassandra and lands in /var/log/cassandra/debug.log (NOT in `kubectl logs`/system.log, which are
INFO-filtered).

## Setup
```
kubectl create namespace repro-14496
# cass-bug: cassandra:4.0.0 (buggy)   -- single pod, pinned to kind-worker
# cass-fix: cassandra:4.0.20 (fixed)  -- single pod, pinned to kind-worker, imagePullPolicy IfNotPresent
# (4.0.1 image -> ImagePullBackOff 429 Too Many Requests from Docker Hub; substituted 4.0.20)
```
Versions confirmed:
```
$ kubectl exec -n repro-14496 cass-bug -- cqlsh -e "SELECT release_version FROM system.local"
 release_version
-----------------
           4.0.0
$ kubectl exec -n repro-14496 cass-fix -- cqlsh -e "SELECT release_version FROM system.local"
 release_version
-----------------
          4.0.20
```

## Reproducer (IDENTICAL on both pods)
```sql
CREATE KEYSPACE IF NOT EXISTS repro14496_ks WITH replication = {'class':'SimpleStrategy','replication_factor':1};

-- table with unchecked_tombstone_compaction=true and NO tombstone_threshold / NO tombstone_compaction_interval
CREATE TABLE repro14496_ks.twcs2 (
  id text, ts timestamp, val text, PRIMARY KEY (id, ts)
) WITH compaction = {
  'class':'TimeWindowCompactionStrategy',
  'compaction_window_unit':'DAYS',
  'compaction_window_size':'1',
  'unchecked_tombstone_compaction':'true'
};
```
(The CREATE was run via `cqlsh --request-timeout=60`. A harmless client-side OperationTimedOut can occur on
a freshly started node during schema agreement; the server completes the DDL regardless, confirmed by
DESCRIBE below.)

Table options are IDENTICAL on both versions (note `'unchecked_tombstone_compaction': 'true'`, and the
ABSENCE of tombstone_threshold / tombstone_compaction_interval):
```
# cass-bug (4.0.0)
AND compaction = {'class': 'org.apache.cassandra.db.compaction.TimeWindowCompactionStrategy', 'compaction_window_size': '1', 'compaction_window_unit': 'DAYS', 'max_threshold': '32', 'min_threshold': '4', 'unchecked_tombstone_compaction': 'true'}
# cass-fix (4.0.20)
AND compaction = {'class': 'org.apache.cassandra.db.compaction.TimeWindowCompactionStrategy', 'compaction_window_size': '1', 'compaction_window_unit': 'DAYS', 'max_threshold': '32', 'min_threshold': '4', 'unchecked_tombstone_compaction': 'true'}
```

## VERBATIM BUGGY SIGNATURE  (cass-bug, cassandra:4.0.0)
Grep of /var/log/cassandra/debug.log — the lines from the twcs2 CREATE (MigrationStage:1, latest timestamp):
```
DEBUG [MigrationStage:1] 2026-06-12 04:24:44,927 TimeWindowCompactionStrategy.java:65 - Disabling tombstone compactions for TWCS
DEBUG [MigrationStage:1] 2026-06-12 04:24:44,927 TimeWindowCompactionStrategy.java:65 - Disabling tombstone compactions for TWCS
```
The earlier twcs_tbl CREATE produced the same on 4.0.0:
```
DEBUG [MigrationStage:1] 2026-06-12 04:20:15,861 TimeWindowCompactionStrategy.java:65 - Disabling tombstone compactions for TWCS
DEBUG [MigrationStage:1] 2026-06-12 04:20:15,862 TimeWindowCompactionStrategy.java:65 - Disabling tombstone compactions for TWCS
```
=> Buggy 4.0.0 DISABLES tombstone compactions even though unchecked_tombstone_compaction=true. This is the bug.

## A/B CONTROL  (cass-fix, cassandra:4.0.20 — fix present)
Grep of /var/log/cassandra/debug.log — the lines from the SAME twcs2 CREATE (MigrationStage:1, latest timestamp):
```
DEBUG [MigrationStage:1] 2026-06-12 04:24:51,730 TimeWindowCompactionStrategy.java:67 - Enabling tombstone compactions for TWCS
DEBUG [MigrationStage:1] 2026-06-12 04:24:51,731 TimeWindowCompactionStrategy.java:67 - Enabling tombstone compactions for TWCS
```
The earlier twcs_tbl CREATE produced the same on 4.0.20:
```
DEBUG [MigrationStage:1] 2026-06-12 04:20:47,762 TimeWindowCompactionStrategy.java:67 - Enabling tombstone compactions for TWCS
DEBUG [MigrationStage:1] 2026-06-12 04:20:47,762 TimeWindowCompactionStrategy.java:67 - Enabling tombstone compactions for TWCS
```
=> Fixed 4.0.20 ENABLES tombstone compactions for the identical table — the patched constructor honors
`unchecked_tombstone_compaction=true`.

(Startup system-table TWCS instantiations at 04:15:49 / 04:18:33 etc. log "Disabling" on BOTH versions
because they carry none of the relevant options — expected, not part of the repro. The discriminating
lines are the user-table CREATEs on MigrationStage:1 at the latest timestamps, which match exactly 1:1.)

## A/B summary (identical input, only the version differs)
| version       | table options (unchecked=true, no threshold/interval) | TWCS instantiation log                       |
|---------------|-------------------------------------------------------|----------------------------------------------|
| 4.0.0 (buggy) | identical                                             | `Disabling tombstone compactions for TWCS` (TimeWindowCompactionStrategy.java:65) |
| 4.0.20 (fix)  | identical                                             | `Enabling tombstone compactions for TWCS`  (TimeWindowCompactionStrategy.java:67) |

The source line shift (65 vs 67) further confirms the patched code path on 4.0.20.

## Conclusion
REPRODUCED. On buggy cassandra:4.0.0, a TWCS table created with `unchecked_tombstone_compaction=true` and
no tombstone_threshold/interval has tombstone compactions silently DISABLED — verbatim
`Disabling tombstone compactions for TWCS`. The fixed cassandra:4.0.20 logs `Enabling tombstone compactions
for TWCS` for the byte-for-byte identical table. tag_correction = none (hint matched body).

## Tooling findings
- cassandra:4.0.1 (the canonical first fixed image) is NOT preloaded on the kind nodes and pulling it from
  Docker Hub failed with HTTP 429 "Too Many Requests / unauthenticated pull rate limit". Only 4.0.0 and
  4.0.20 are preloaded (and only on node kind-worker). Any harness step that assumes <buggy-patch+1> images
  are pullable on demand will fail under Docker Hub rate limiting; preloading the exact fixed-control images
  (or using an authenticated/cached registry) would make A/B controls robust.
- The default cqlsh client request timeout (10s) is too short for DDL on a just-started Cassandra node and
  raises OperationTimedOut even though the server applies the schema; `--request-timeout=60` avoids the
  spurious error.
