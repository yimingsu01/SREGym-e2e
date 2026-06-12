# CASSANDRA-14349 — Untracked CDC segment files are not deleted after replay

- **Buggy version:** cassandra:3.11.9 (deployed as pod `cass-buggy`)
- **Control image attempted:** cassandra:3.11.10 (pod `cass-fixed`) — see CONTROL section: NOT actually fixed.
- **Namespace:** repro-14349 (kind, context kind-kind). Single-node pods, ephemeral container FS.
- **Keyspace:** repro14349 (non-CDC table `t`)
- **Disposition:** reproduced (resource-leak bug → evidence is filesystem state, not a stack trace).

## Primary source (Jira ground truth)
fixVersions=[3.11.10, 4.0-rc2]; components=[Legacy/Local Write-Read Paths]; Status=Resolved/Fixed; resolved 27/Jul/2021.

> "When CDC is enabled, a hard link to each commit log file will be created in cdc_raw directory... if we don't produce any CDC traffic, those hard links in cdc_raw will be never cleaned up... whereas the real original commit logs are correctly deleted after replay during process startup. This will results in many untracked hard links in cdc_raw if we restart the cassandra process many times... This seems a bug in handleReplayedSegment of the commit log segment manager which neglects to take care of CDC commit logs."

## Exact reproducer extracted
1. Enable CDC (`cdc_enabled: true` in cassandra.yaml).
2. Produce NO CDC traffic (write only to a non-CDC table to guarantee commit-log content).
3. Kill -9 the Cassandra process (NOT drain) so dirty commit-log segments must be REPLAYED on next start.
4. Restart repeatedly. Bug = replayed commit-log segments pile up in `cdc_raw/` and are never deleted,
   while the live `commitlog/` correctly rotates/deletes them.

Mechanism note: `CommitLogSegmentManagerCDC.handleReplayedSegment(File)` unconditionally does
`renameWithConfirm(file -> cdc_raw/)` + `cdcSizeTracker.addFlushedSize(...)` with NO deletion / no
"is there CDC data?" check. So every replayed segment is relocated into cdc_raw and never removed.
(Verbatim from apache/cassandra cassandra-3.11 branch, still present at HEAD=3.11.20.)

## Deploy method (data dir MUST persist across restarts)
Pods run a no-op (`sed cdc_enabled:true; sleep infinity`) so the container FS (incl.
/var/lib/cassandra) survives. Cassandra is started/killed via `kubectl exec` (setsid + docker-entrypoint),
NOT by recreating the pod (which would wipe cdc_raw and mask the leak). Images side-loaded into kind via
`ctr import --digests=false` (Docker Hub 429 rate-limited; host had the images).

cdc_enabled confirmed true at runtime on both pods; `nodetool version` => buggy 3.11.9, control 3.11.10.

## BUGGY 3.11.9 — kill-restart loop (the reproduction)

Baseline after cycle-1 bootstrap: `cdc_raw` empty.
```
CDC_LOG_COUNT=0
COMMITLOG_COUNT=2
```

Each cycle = kill -9 + restart; replay confirmed each time, e.g.:
```
INFO  [main] 2026-06-12 05:23:45,542 CommitLog.java:147 - Replaying /opt/cassandra/data/commitlog/CommitLog-6-1781241743009.log, /opt/cassandra/data/commitlog/CommitLog-6-1781241743010.log
INFO  [main] 2026-06-12 05:23:47,440 CommitLog.java:149 - Log replay complete, 28 replayed mutations
```

Monotonic accumulation of orphaned segments in cdc_raw (originals already deleted from commitlog):
```
CDC_LOG_COUNT after cycle1(bootstrap) = 0
CDC_LOG_COUNT after cycle2            = 2
CDC_LOG_COUNT after cycle3            = 4
CDC_LOG_COUNT after cycle4            = 6
```

Final cdc_raw listing (6 leaked segments, each 32MB logical):
```
-rw-r--r-- 1 cassandra cassandra 33554432 Jun 12 05:23 CommitLog-6-1781241743009.log
-rw-r--r-- 1 cassandra cassandra 33554432 Jun 12 05:22 CommitLog-6-1781241743010.log
-rw-r--r-- 1 cassandra cassandra 33554432 Jun 12 05:23 CommitLog-6-1781241821228.log
-rw-r--r-- 1 cassandra cassandra 33554432 Jun 12 05:23 CommitLog-6-1781241821229.log
-rw-r--r-- 1 cassandra cassandra 33554432 Jun 12 05:24 CommitLog-6-1781241861343.log
-rw-r--r-- 1 cassandra cassandra 33554432 Jun 12 05:24 CommitLog-6-1781241861344.log
```

Live commitlog at the same moment holds only the 2 CURRENT segments (originals correctly deleted):
```
-rw-r--r-- 1 cassandra cassandra 33554432 Jun 12 05:25 CommitLog-6-1781241912844.log
-rw-r--r-- 1 cassandra cassandra 33554432 Jun 12 05:25 CommitLog-6-1781241912845.log
```

ORPHAN CHECK — every cdc_raw entry is absent from commitlog and is the sole remaining copy (linkcount=1):
```
CommitLog-6-1781241743009.log  linkcount=1  ORPHAN(absent-from-commitlog)
CommitLog-6-1781241743010.log  linkcount=1  ORPHAN(absent-from-commitlog)
CommitLog-6-1781241821228.log  linkcount=1  ORPHAN(absent-from-commitlog)
CommitLog-6-1781241821229.log  linkcount=1  ORPHAN(absent-from-commitlog)
CommitLog-6-1781241861343.log  linkcount=1  ORPHAN(absent-from-commitlog)
CommitLog-6-1781241861344.log  linkcount=1  ORPHAN(absent-from-commitlog)
```
(linkcount=1 is expected: handleReplayedSegment MOVES the file via renameWithConfirm; after replay the
original commitlog path is gone, so the cdc_raw copy is the only remaining reference. The Jira's "hard
link" wording refers to the broader CDC mechanism; the leak symptom — replayed segments relocated into
cdc_raw and never deleted — is exactly what is observed.)

du of cdc_raw: 316K actual (sparse 32MB files).

## VERBATIM BUGGY SIGNATURE
```
CommitLog-6-1781241743009.log  linkcount=1  ORPHAN(absent-from-commitlog)
```
plus the monotonic `CDC_LOG_COUNT` progression 0 -> 2 -> 4 -> 6 across kill-restart cycles on 3.11.9.

## CONTROL — A/B attempted on cassandra:3.11.10, found INVALID
Ran the IDENTICAL kill-restart loop on cass-fixed (3.11.10). It behaved IDENTICALLY (also leaked):
```
FIXED CDC_LOG_COUNT after cycle2 = 2
FIXED CDC_LOG_COUNT after cycle3 = 4
FIXED CDC_LOG_COUNT after cycle4 = 6
```
Fixed cdc_raw orphan check (identical pattern, all linkcount=1, absent from commitlog).

Reason the A/B does not discriminate: **cassandra:3.11.10 does NOT contain the 14349 fix.**
- Released 3.11.10 binary CHANGES.txt is dated `Jan 29 2021`; the 3.11.10 section lists NO CASSANDRA-14349.
  `grep 14349 /opt/cassandra/CHANGES.txt` => exit 1 (absent) in the 3.11.10 image.
- The Jira was resolved `27/Jul/2021` — months AFTER 3.11.10 shipped. The "fixVersion 3.11.10" label is
  inaccurate for the actual released artifact.
- The buggy `handleReplayedSegment` (rename-into-cdc_raw, no cleanup) is STILL present on the current
  apache/cassandra cassandra-3.11 branch (HEAD CHANGES.txt top = 3.11.20); 14349 not in cassandra-4.0
  CHANGES.txt either.

=> No valid fixed 3.11.x image exists. Discrimination rests on: (a) primary-source mechanism match,
(b) source inspection of handleReplayedSegment, (c) empirical proof the candidate "fixed" image is
unfixed. This is the task's "within-version reasoning" path, not a clean A/B.

## CDC-GATED DISCRIMINATOR (3.11.9, cdc_enabled:false, identical loop)
Same buggy binary (3.11.9), only difference = `cdc_enabled: false`. cdc_raw cleared to 0, then the
IDENTICAL kill-restart loop run 3x with replay firing each time:
```
restart(cdc-off): Log replay complete, 95 replayed mutations   -> cdc_raw = 0
cdc-off cycleA:    Log replay complete, 69 replayed mutations   -> cdc_raw = 0
cdc-off cycleB:    Log replay complete, 47 replayed mutations   -> cdc_raw = 0
```
=> With CDC OFF the leak does NOT occur (0 across all restarts) even though replay fires every cycle.
This demonstrates the accumulation is CDC-gated (caused by CDC being enabled), not a generic
restart/replay artifact. Combined with the cdc-ON result (0->2->4->6 on the SAME binary), this is a
controlled within-version discriminator for CASSANDRA-14349.

SUMMARY MATRIX (kill-restart loop, replay confirmed each cycle):
- 3.11.9  + CDC on  : 0 -> 2 -> 4 -> 6   (LEAK reproduced)
- 3.11.9  + CDC off : 0 -> 0 -> 0        (no leak; same binary)  <- CDC-gated discriminator
- 3.11.10 + CDC on  : 0 -> 2 -> 4 -> 6   (still leaks; 3.11.10 NOT fixed)
