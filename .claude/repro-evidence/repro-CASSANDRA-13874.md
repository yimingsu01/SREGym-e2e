# CASSANDRA-13874 Reproduction Evidence Log

## Issue
- **Key**: CASSANDRA-13874
- **Summary**: nodetool setcachecapacity behaves oddly when cache disabled
- **Description (primary source / ground truth)**: "If a node has row cache disabled, trying to turn it on via setcachecapacity doesn't issue an error, and doesn't turn it on, it just silently doesn't work."
- **fixVersions**: 3.11.12, 4.0.1, 4.1-alpha1, 4.1
- **Components**: Legacy/Core, Local/Config
- **Buggy version under test**: cassandra:4.0.0
- **Classifier hint**: topology=1node, confidence=M, trigger="row cache disabled + nodetool setcachecapacity to enable it -> silent no-op, no error" -> MATCHES the body. tag_correction = none.

## Reproducer (extracted from body)
1. Start single-node Cassandra with row cache DISABLED. This is the stock default
   (`row_cache_size_in_mb: 0`), so no cassandra.yaml override is needed.
2. Run `nodetool setcachecapacity <key_MB> <row_MB> <counter_MB>` asking for a non-zero row cache.
3. Observe: command exits 0 with no error/stderr, and `nodetool info` shows Row Cache capacity STILL 0 bytes.
   The within-node control is that the SAME command DOES update Key Cache and Counter Cache capacities.

## Topology
Single node (1node). Deployed as pod `cass-400` (image cassandra:4.0.0) in namespace `repro-13874`
on the existing kind-kind cluster. A second pod `cass-401` (cassandra:4.0.1) was deployed as the
intended fixed-image A/B control.

## Environment confirmation (startup logs, both pods)
Stock config confirms row cache disabled at boot:
```
INFO  CacheService.java:100 - Initializing key cache with capacity of 49 MBs.
INFO  CacheService.java:122 - Initializing row cache with capacity of 0 MBs
INFO  CacheService.java:151 - Initializing counter cache with capacity of 24 MBs
```
Config line: `row_cache_class_name=org.apache.cassandra.cache.OHCProvider; row_cache_size_in_mb=0`

================================================================
## BUGGY 4.0.0 (cass-400) -- VERBATIM REPRODUCTION

```
$ kubectl exec -n repro-13874 cass-400 -- nodetool version
ReleaseVersion: 4.0.0

--- PRE-CHECK: nodetool info (Cache lines) ---
Key Cache              : entries 10, size 896 bytes, capacity 49 MiB, 66 hits, 80 requests, 0.825 recent hit rate, 14400 save period in seconds
Row Cache              : entries 0, size 0 bytes, capacity 0 bytes, 0 hits, 0 requests, NaN recent hit rate, 0 save period in seconds
Counter Cache          : entries 0, size 0 bytes, capacity 24 MiB, 0 hits, 0 requests, NaN recent hit rate, 7200 save period in seconds

--- RUN: nodetool setcachecapacity 200 50 50  (key=200MB row=50MB counter=50MB) ---
EXIT_CODE=0        # silent, no stdout, no stderr

--- POST-CHECK: nodetool info (Cache lines) ---
Key Cache              : entries 10, size 896 bytes, capacity 200 MiB, 66 hits, 80 requests, 0.825 recent hit rate, 14400 save period in seconds
Row Cache              : entries 0, size 0 bytes, capacity 0 bytes, 0 hits, 0 requests, NaN recent hit rate, 0 save period in seconds
Counter Cache          : entries 0, size 0 bytes, capacity 50 MiB, 0 hits, 0 requests, NaN recent hit rate, 7200 save period in seconds
```

### Interpretation (the bug, exactly as the Jira body describes)
The operator asked to enable a 50 MB row cache. The command returned EXIT 0 with NO error and NO stderr.
The within-node control proves it is specifically the disabled-row-cache path:
- Key Cache capacity:     49 MiB  -> 200 MiB   (UPDATED -- enabled by default)
- Counter Cache capacity: 24 MiB  ->  50 MiB   (UPDATED -- enabled by default)
- Row Cache capacity:      0 bytes ->   0 bytes (SILENTLY NOT UPDATED -- the bug)

Same single command; two caches changed, the row cache silently did nothing and reported no error.
Confirmed reproducible: a second run `nodetool setcachecapacity 100 30 30` was also silent (EXIT=0,
empty stderr) and left Row Cache at 0 bytes.

### VERBATIM BUGGY SIGNATURE (wrong-result line, literal copy)
```
Row Cache              : entries 0, size 0 bytes, capacity 0 bytes, 0 hits, 0 requests, NaN recent hit rate, 0 save period in seconds
```
(after `nodetool setcachecapacity 200 50 50` returned EXIT 0 with empty stderr -- i.e. the requested
50 MB row cache was silently NOT applied and no error was raised)

================================================================
## CONTROL

### A/B with cassandra:4.0.1 (Jira fixVersion) -- FAILED: image lacks the fix
Identical workload on cass-401 (cassandra:4.0.1) behaved IDENTICALLY to the buggy 4.0.0:
```
$ kubectl exec -n repro-13874 cass-401 -- nodetool version
ReleaseVersion: 4.0.1
$ kubectl exec -n repro-13874 cass-401 -- nodetool setcachecapacity 200 50 50   # EXIT=0, empty stderr
--- POST nodetool info ---
Key Cache              : ... capacity 200 MiB ...
Row Cache              : entries 0, size 0 bytes, capacity 0 bytes, ...     # STILL 0 -- no error either
Counter Cache          : ... capacity 50 MiB ...
```

Root cause established by inspecting the fix and the shipped jars:
- The fix (commit 957c6264ef97909a043a70b96cf896b1feb0f204) adds a guard to
  `NopCacheProvider$NopCache.setCapacity(long)` that throws
  `UnsupportedOperationException("Setting capacity of NopCache is not permitted as this cache is disabled. Check your yaml settings if you want to enable it.")`
  for any non-zero capacity.
- Searching the shipped jars (via python zipfile, no unzip/jar in container):
  - `apache-cassandra-4.0.0.jar`: NopCacheProvider present, fix string ABSENT.
  - `apache-cassandra-4.0.1.jar` (built 2021-08-30): NopCacheProvider present, fix string ABSENT.
  => The cassandra:4.0.1 Docker image does NOT contain the 13874 fix even though Jira lists fixVersion
     4.0.1; the commit landed in a later 4.0.x build. So the buggy-patch+1 == fixed-image assumption fails
     for this issue. (Recorded in tooling_findings.)

### Within-version control (used as the authoritative control)
Holding the binary constant (cassandra:4.0.0), the SAME `setcachecapacity` call updated Key Cache
(49->200 MiB) and Counter Cache (24->50 MiB) while Row Cache stayed at 0 bytes. This isolates the defect
to the disabled-row-cache capacity path -- it is not a dead command. This is a stronger control than a
cross-version A/B because nothing but the target cache differs.

### Gold-standard control: decisive bytecode contrast across 4.0.0 / 4.0.1 / 4.0.2
Parallel probe sleep-pods were used to locate the first 4.0.x build that contains the fix, then the
shipped `NopCacheProvider$NopCache.class` was inspected in each jar (python zipfile + string scan; no
unzip/jar/javap in the containers). The guard string the fix introduces is
`is not permitted as this cache is disabled. Check your yaml settings if you want to enable it.`

```
cass-400  (cassandra 4.0.0) : NO GUARD  (buggy: setCapacity is a silent no-op)
cass-401  (cassandra 4.0.1) : NO GUARD  (buggy: setCapacity is a silent no-op)   <- Jira lists 4.0.1 as fixVersion, but the image does NOT have the fix
probe-402 (cassandra 4.0.2) : FIX GUARD PRESENT: "_ is not permitted as this cache is disabled. Check your yaml settings if you want to enable it."
```

This bytecode contrast exactly corroborates the runtime results: 4.0.0 and 4.0.1 both silently no-op the
row-cache capacity change (identical observed behavior), while the throwing guard only exists from 4.0.2
onward. It is a clean A/B control on the exact defective method.

### Runtime A/B on 4.0.2 (the throwing UnsupportedOperationException): BLOCKED at this scale
Attempting to boot a real cassandra:4.0.2 pod to capture the live exception was blocked by:
- Docker Hub 429 ("You have reached your unauthenticated pull rate limit") on a fresh `cass-402` pod
  (image not pre-pulled on the assigned node).
- The already-pulled `probe-402` pod has `limits: memory: 256Mi`, far below what Cassandra needs to
  boot (1024M heap) -> Cassandra cannot start there.
The bytecode contrast above is the authoritative control in lieu of the live exception; the live
`UnsupportedOperationException("Setting capacity of NopCache is not permitted ...")` is the expected
4.0.2+ behavior per the fix commit and the present guard string.

## Disposition
**reproduced** -- verbatim operator-visible wrong result captured on the buggy image (row cache silently
stays at 0 bytes after a non-zero setcachecapacity, with EXIT 0 and no error), exactly matching the Jira
body. tag_correction = none.
