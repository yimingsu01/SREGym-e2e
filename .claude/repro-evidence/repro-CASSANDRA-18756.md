# CASSANDRA-18756 — Reproduction Evidence Log

**Title:** TimeWindowCompactionStrategy with `unsafe_aggressive_sstable_expiration` keeps overlapping SSTable references
**Buggy version (assigned):** 4.1.3   **Fix versions:** 3.11.17, 4.0.12, **4.1.4**, 5.0-alpha2, 5.0, 6.0
**Component:** Local/Compaction
**Disposition:** `confirmed-blocked` (needs in-JVM / concurrent-compaction timing instrumentation)
**Namespace:** repro-18756   **Keyspace:** ks18756   **Topology:** single node (1 pod)

---

## 1. Primary source (ground truth from Jira body)

> When `unsafe_aggressive_sstable_expiration` is turned on, TWCS should not create or maintain an
> iterator of overlapping sstables. However, because `TimeWindowCompactionController` inherits from
> `CompactionController` and only sets `ignoreOverlaps` after the base class has constructed the overlap
> iterator, it ends up making an overlap iterator and then never updating it.
> The end result is that such a compaction keeps references to lots of and likely _all_ other SSTables
> on the node and thus delays the deletion of obsolete ones by hours or even days.

So the real symptom is **delayed *release/deletion* of references to obsolete SSTables**, NOT "fully-expired
SSTables are never dropped". (The classifier hint "obsolete SSTables never deleted" is imprecise — see §6.)

## 2. Exact mechanism — confirmed from the fix diff (commit 87c2af85, "Fix delayed SSTable release with unsafe_aggressive_sstable_expiration", patch by Ethan Brown, CASSANDRA-18756)

`CompactionController` parent constructor calls `refreshOverlaps()` (line 84). The `ignoreOverlaps()`
method is **overridden** in `TimeWindowCompactionController` to return its `this.ignoreOverlaps` field —
but that field is assigned only AFTER `super(...)` returns. So during construction `ignoreOverlaps()`
returns the default `false`, and `refreshOverlaps()` takes the `else` branch and references **all
overlapping live SSTables**, building an overlap iterator that holds Refs to them.

Then `maybeRefreshOverlaps()` (called periodically from `CompactionTask.runMayThrow`) short-circuited in
the buggy code: `if (ignoreOverlaps()) { ...debug...; return; }` — now `ignoreOverlaps()` returns `true`,
so the stale overlap reference set is **never refreshed/released** for the life of the compaction →
references to obsolete SSTables are held → their deletion is delayed by hours/days.

### Fix diff (verbatim, what 4.1.4 changed vs 4.1.3):
```diff
 public CompactionController(... )
 {
+    //When making changes to the method, be aware that some of the state of the controller may still be uninitialized
+    //(e.g. TWCS sets up the value of ignoreOverlaps() after this completes)
     ...
 }

 public void maybeRefreshOverlaps()
 {
     ...
-    if (ignoreOverlaps())
-    {
-        logger.debug("not refreshing overlaps - running with ignoreOverlaps activated");
-        return;
-    }
     for (SSTableReader reader : overlappingSSTables) { if (reader.isMarkedCompacted()) { refreshOverlaps(); return; } }
 }

 void refreshOverlaps()
 {
-    if (compacting == null || ignoreOverlaps())
+    if (compacting == null)
         overlappingSSTables = Refs.tryRef(Collections.<SSTableReader>emptyList());
     else
         overlappingSSTables = cfs.getAndReferenceOverlappingLiveSSTables(compacting);
     ...
 }
 protected boolean ignoreOverlaps()   // javadoc gained: "Do NOT call this method in the CompactionController constructor"
```
NOTE: the static `getFullyExpiredSSTables(cfStore, compacting, overlapping, gcBefore, ignoreOverlaps)`
logic is **UNCHANGED** between 4.1.3 and 4.1.4. Therefore a black-box test built around
"fully-expired SSTables not dropped" would behave identically on both images and is the wrong reproducer.

## 3. Why this is `confirmed-blocked` (the fix's own regression test settles it)

The fix's regression test `CompactionControllerTest.testOverlapIterator` (added in the same commit) needs:
- **Byteman / BMUnit byte-code injection** (`@RunWith(BMUnitRunner.class)`, `@BMRules`) to (a) pause
  `compaction1` mid-flight at `INVOKE getCompactionAwareWriter`, (b) count calls to
  `ColumnFamilyStore.getAndReferenceOverlappingLiveSSTables`, and (c) block at `INVOKE finish`.
- **Two concurrent compaction threads** + four `CountDownLatch`es to deterministically interleave a
  *second* compaction that obsoletes the SSTables held by the first compaction's stale overlap refs.
- The asserted invariant is `overlapRefreshCounter == 2` (refresh must happen again after compaction2),
  i.e. the held references must be **released**.

The symptom is invisible at steady state: once any compaction finishes, its Refs are released and a
post-hoc `nodetool` / `ls` / `sstableutil` snapshot cannot distinguish buggy from fixed. The lingering
only exists *while one long compaction runs and a concurrent op obsoletes those SSTables*. That precise
interleaving is not stageable from a black-box Cassandra pod in kind — it requires in-JVM message/timing
interception, which is the named blocker.

## 4. Empirical confirmation on a real pod (buggy 4.1-line code)

> NOTE ON IMAGE: the assigned buggy image `cassandra:4.1.3` could NOT be pulled — Docker Hub returned
> HTTP 429 (unauthenticated pull-rate limit) and 4.1.3 was not cached on any kind node. The buggy code
> is byte-identical on the 4.1 line: `diff` of `CompactionController.java` between cassandra-4.1.1 and
> cassandra-4.1.3 shows ONLY one cosmetic change (`private void refreshOverlaps()` -> `void refreshOverlaps()`);
> the buggy `maybeRefreshOverlaps()` short-circuit and `refreshOverlaps()` `|| ignoreOverlaps()` are
> IDENTICAL. The locally-cached `cassandra:4.1.1` was therefore used as a faithful carrier of the buggy
> 4.1-line path. (4.1.4 is the first 4.1 release with the fix.)

### Pod
`cassandra:4.1.1`, namespace `repro-18756`, pinned to kind-worker2 (image cached), launched with
`JVM_EXTRA_OPTS=-Dcassandra.allow_unsafe_aggressive_sstable_expiration=true` (verified in /proc/1/cmdline).

### Schema (the table option is GATED — it round-trips only because the JVM property is set)
```
CREATE TABLE ks18756.twcs (k int, c int, v text, PRIMARY KEY (k,c))
  WITH default_time_to_live=30 AND gc_grace_seconds=0
  AND compaction={'class':'TimeWindowCompactionStrategy','compaction_window_unit':'MINUTES',
                  'compaction_window_size':'1','unsafe_aggressive_sstable_expiration':'true',
                  'expired_sstable_check_frequency_seconds':'0'};
```
`DESCRIBE TABLE` confirms the option was accepted:
```
unsafe_aggressive_sstable_expiration': 'true'
```
(Without `-Dcassandra.allow_unsafe_aggressive_sstable_expiration=true`, CREATE would throw
`ConfigurationException: ... restart cassandra with -Dcassandra.allow_unsafe_aggressive_sstable_expiration=true to allow it`.)

### VERBATIM buggy-path signature captured at runtime (system.log)
This WARN is emitted by `TimeWindowCompactionController` constructor ONLY when `ignoreOverlaps==true`,
i.e. it proves the buggy overlap-ignoring controller path executed:
```
WARN  [CompactionExecutor:2] 2026-06-12 03:37:59,995 TimeWindowCompactionController.java:41 - You are running with sstables overlapping checks disabled, it can result in loss of data
WARN  [CompactionExecutor:3] 2026-06-12 03:38:11,313 TimeWindowCompactionController.java:41 - You are running with sstables overlapping checks disabled, it can result in loss of data
WARN  [CompactionExecutor:4] 2026-06-12 03:40:24,514 TimeWindowCompactionController.java:41 - You are running with sstables overlapping checks disabled, it can result in loss of data
```

### Attempt to capture the fix-removed DEBUG line (`"not refreshing overlaps - running with ignoreOverlaps activated"`)
Set `nodetool setlogginglevel org.apache.cassandra.db.compaction.CompactionController DEBUG` (verified via
`getlogginglevels`). Wrote 9 overlapping SSTables in one time window (autocompaction disabled), throttled
`nodetool setcompactionthroughput 1` (1 MB/s), ran `nodetool compact ks18756 twcs`, watched debug.log.

RESULT — the DEBUG line was NOT emitted:
```
grep -c 'not refreshing overlaps - running with ignoreOverlaps activated' debug.log  ->  0
```
ROOT CAUSE (confirmed in source, CompactionTask.java:209): `maybeRefreshOverlaps()` is only called when
`nanoTime() - lastCheckObsoletion > TimeUnit.MINUTES.toNanos(1L)` — i.e. **only inside a compaction that
runs longer than 60 seconds**. Even 9 SSTables at the minimum 1 MB/s throughput compacted in << 60s
(dataset on disk ~1.3 MB after compression), so the 1-minute checkpoint never tripped. Producing a
>60s single compaction would need GBs of incompressible data, infeasible in a 2.5 GiB pod within budget —
and even then it would only show the *holding* of refs, not the *delayed-deletion* symptom, which still
requires the concurrent-obsoletion interleaving of §3.

## 5. A/B control
- Disk-symptom A/B (4.1.x-buggy vs 4.1.4-fixed) does NOT discriminate at steady state — by §2 the
  fully-expired-drop path is identical, and the ref-holding is invisible once any compaction ends.
- Source-level A/B: the buggy short-circuit lines in `maybeRefreshOverlaps()` / `refreshOverlaps()` are
  PRESENT in 4.1.1/4.1.3 and REMOVED in 4.1.4 (diff in §2). The runtime WARN of §4 is present on the
  buggy image; on 4.1.4 the controller still logs that same WARN (constructor unchanged), so the WARN is
  NOT itself a fix-discriminating signal — it only proves the ignoreOverlaps path is active. The
  discriminating change is internal (overlap-ref release timing) and only observable via the Byteman test.

## 6. Tag correction
- topology=1node: CORRECT (Local/Compaction; no ring needed).
- confidence=H: WRONG. The hint implies an easy black-box repro; reality requires in-JVM Byteman
  interception + two concurrent compactions + a >60s compaction window (per the fix's own regression test).
- Hint trigger wording "obsolete SSTables never deleted": imprecise — deletion is *delayed* (until the
  next overlap refresh / compaction releases the Refs), and only under concurrent compaction, not "never".

## 7. Tooling findings
- `cassandra:4.1.3` (the assigned buggy image) is not cached on any kind node and Docker Hub returns
  HTTP 429 (unauthenticated pull-rate limit). Repro proceeded on `cassandra:4.1.1` (byte-identical buggy
  code on the 4.1 line, see §4). If the harness needs the exact buggy tag, it should pre-pull/cache
  4.1.3 (and 4.1.4 for the control) or use an authenticated registry mirror. RECORD-ONLY; not fixed.
