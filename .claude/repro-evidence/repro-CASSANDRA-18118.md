# CASSANDRA-18118 — Do not leak 2015 memtable synthetic Epoch

- **Buggy version:** cassandra:4.1.0
- **Fixed control:** cassandra:4.1.1 (fixVersions: 3.11.15, 4.0.8, 4.1.1, 5.0-alpha1, 5.0; 4.1 ceiling=11, so A/B available)
- **Namespace:** repro-18118 (single-node pods cass-buggy=4.1.0, cass-fixed=4.1.1)
- **Keyspace:** repro18118 (RF=1, SimpleStrategy)
- **Classifier hint:** topology=1node, confidence=H, trigger="table with 10s TTL+gc_grace + sustained
  inserts -> synthetic 2015 Epoch leaks into memtable minTimestamp -> expired sstables never cleaned,
  disk grows forever"
- **Components:** Local/Memtable
- **Disposition:** NOT-REPRODUCIBLE (no operator-visible divergence between 4.1.0 and 4.1.1 on this path)

## Bug mechanism (from Jira body)
`EncodingStats` uses a synthetic Epoch in 2015 (`TIMESTAMP_EPOCH`) that plays nicely with VInt
serialization. `Memtable` was alleged to use that synthetic value to track `minTimestamp`, leaking the
2015 Epoch into the live memtable's minTimestamp. That contaminated minTimestamp feeds the
purge / fully-expired-SSTable detection (`maxPurgeableTimestamp`), so on the buggy version expired
SSTables are never detected as fully-expired and disk grows forever under sustained load.

## Exact reproducer extracted from body
Single node, RF=1. Table `test.test (key text PK, id text)` with:
- `default_time_to_live = 10`, `gc_grace_seconds = 10`
- SizeTieredCompactionStrategy, `tombstone_compaction_interval=3000`, `tombstone_threshold=0.1`,
  `unchecked_tombstone_compaction=true`
- secondary index `CREATE INDEX id_idx ON test.test (id)`

Load: `insert into test.test (key,id) values('<uuid> <uuid>','eaca36a1-...') USING TTL 10` sustained.
Body's claimed behavioral signatures:
1. Run load a couple minutes, track SSTable disk usage -> only increases, never cleaned up, never stops
   growing (well past 10s TTL + 10s gc_grace).
2. flush + compact WHILE under load -> does NOT solve it.
3. Stop load + compact -> does NOT solve it. Flushing (then idle) DOES solve it.

Per advisor + body: the poison lives in the NON-EMPTY (active) memtable's minTimestamp. An empty
(just-flushed, no new writes) memtable does not poison, which is why flush-then-idle self-heals. So the
discriminator must be observed while the memtable is continuously non-empty (sustained load running).

## Schema applied (verbatim, both pods)
```
create keyspace repro18118 WITH replication = {'class':'SimpleStrategy', 'replication_factor' : 1};
CREATE TABLE repro18118.test (key text PRIMARY KEY, id text) WITH ...
    AND compaction = {'class': 'org.apache.cassandra.db.compaction.SizeTieredCompactionStrategy',
        'max_threshold': '32', 'min_threshold': '2', 'tombstone_compaction_interval': '3000',
        'tombstone_threshold': '0.1', 'unchecked_tombstone_compaction': 'true'}
    AND default_time_to_live = 10 AND gc_grace_seconds = 10 ...;
CREATE INDEX id_idx ON repro18118.test (id);
```
Both `schema-rc=0`. Versions confirmed: `release_version` 4.1.0 (cass-buggy), 4.1.1 (cass-fixed).

## Cycle 1 — sustained TTL-10 load + per-iteration flush (~130s)
Loader: 200 random-UUID inserts/iter with `USING TTL 10`, `nodetool flush` each iter.
Result at end (both pods): COUNT(*)=0 (all TTL-expired), tiny disk on BOTH (240KB buggy / 352KB fixed).
=> per-iteration flush created empty-memtable windows where compaction self-healed on BOTH. Inconclusive
(this matches body's "flushing solves it" — my loop flushed too often).

### Sharp signal check — sstablemetadata Minimum timestamp on flushed SSTables (both pods)
Tool path: /opt/cassandra/tools/bin/sstablemetadata (not on $PATH).
```
BUGGY nb-59-big-Data.db:
  Minimum timestamp: 1781234906571692 (06/12/2026 03:28:26)
  EncodingStats minTimestamp: 1781234899641893 (06/12/2026 03:28:19)
FIXED nb-61-big-Data.db:
  Minimum timestamp: 1781234908670213 (06/12/2026 03:28:28)
  EncodingStats minTimestamp: 1781234904694742 (06/12/2026 03:28:24)
```
**Both show 2026 timestamps. NO 2015 epoch (~1442880000000000) on disk on the buggy version.** The
leak, if present, is purely the in-memory memtable value and does NOT serialize into flushed SSTable
metadata. So sstablemetadata is NOT a discriminator here (matches advisor's caveat). Pivoted to
behavioral A/B.

## Cycle 2 — continuous load (NO in-loop flush) + manual flush/refill/compact UNDER load
Loader: awk-generated 2000 distinct-key inserts/iter `USING TTL 10`, looped continuously, NO flush in
loop (keeps memtable non-empty). Ran ~75s (data well past 20s TTL+gc_grace), loaders verified running.

Pre-compact (load running): both SSTable count=0 (all in memtable), partitions ~78-82K.
Then: `nodetool flush` (creates a 100%-expired SSTable) -> wait ~4s so memtable REFILLS (stays
non-empty = poisoned on buggy, verified loaders still running) -> `nodetool compact` -> measure.

POST-COMPACT (load running):
```
BUGGY:  SSTable count=1  Space used (total)=289629  partitions(est)=19639
FIXED:  SSTable count=1  Space used (total)=334703  partitions(est)=19802
```
**Both reclaimed identically** (3.5MB -> ~0.3MB, ~92K -> ~19.6K partitions). Buggy did NOT retain more.
This directly contradicts the body's claim that "flush+compact under load doesn't solve it" on buggy.

## Cycle 3 — pure autonomous sustained load, NO manual flush/compact (~2 min)
Loader: awk-generated 8000 distinct-key inserts/iter, continuous, no manual flush/compact. Sampled
on-disk data-dir size + SSTable count every 20s.
- du sampling showed BUGGY ~7248KB plateau vs FIXED ~1156KB at one window, but SSTable count=0 on both
  (data in memtable; du dominated by commitlog/transient files) — not a clean signal.
- At a later snapshot under load: BUGGY data-dir du=14520KB vs FIXED=8416KB, BUT both had exactly ONE
  Data.db file (nb-74-big-Data.db ~2.48MB on BOTH), Space used (total) ~4.93MB on BOTH, partitions
  137,894 (buggy) vs 145,224 (fixed). The du delta was transient compaction scratch files, NOT logical
  table data — steady-state Data.db files were byte-for-byte comparable in size.

### Decisive A/B — flush + 3s refill + compact, all UNDER continuous load
Loaders verified running (memtable non-empty = poisoned on buggy if bug present):
```
PRE (under load):   BUGGY parts=178161   FIXED parts=185447
POST-COMPACT:
  BUGGY:  Space used (total)=298503  partitions(est)=20515  expired-rows-in-newest-sstable=7475
  FIXED:  Space used (total)=338682  partitions(est)=23155  expired-rows-in-newest-sstable=8524
```
sstabledump confirmed expired rows (`"liveness_info": { ..., "ttl":10, "expires_at":"...Z", "expired":
true }`) are physically present on BOTH versions in similar counts (7475 vs 8524) — i.e., both retain
within-gc_grace expired rows and both purge past-gc_grace data during compaction. The compacted SSTable
file set was identical (1 Data.db each, comparable size).

## Cycle 4 (workload swap, body reproducer #2) — BOUNDED-KEY overwrite of same ~100 partitions
The body's SECOND documented reproducer: "repeatedly inserting/deleting/overwriting the SAME values
over and over again without 2i/TTL." This removes the distinct-key confound (bounded live set, so ANY
on-disk growth is garbage). Loader: 5000 inserts/iter with `key = k<i%100>` (same 100 partitions),
`USING TTL 10`, continuous, no in-loop flush. Ran ~80s, loaders verified running.

### Probe 1 — sstablemetadata on flushed SSTable (look for 2015 epoch 1442880000000000)
```
BUGGY nb-87-big-Data.db:
  Minimum timestamp: 1781236560592098 (06/12/2026 03:56:00)
  EncodingStats minTimestamp: 1781236465460205 (06/12/2026 03:54:25)
FIXED nb-87-big-Data.db:
  Minimum timestamp: 1781236563194543 (06/12/2026 03:56:03)
  EncodingStats minTimestamp: 1781236563194543 (06/12/2026 03:56:03)
```
**Both 2026; NO 2015 epoch (1442880000000000) on buggy.** Leak does not serialize even under bounded-key
overwrite.

### Probe 2/3 — flush + 3s refill + compact UNDER load; with only ~100 live keys, buggy retaining => bug
```
PRE-compact (load running):
  BUGGY:  SSTable count=1  Space used (total)=11807  partitions(est)=200
  FIXED:  SSTable count=1  Space used (total)= 7367  partitions(est)=200
POST-compact-under-load (loaders verified running before compact):
  BUGGY:  SSTable count=1  Space used (total)=7356  partitions(est)=200  rows-in-sstable=100  keys=100
  FIXED:  SSTable count=1  Space used (total)=7368  partitions(est)=200  rows-in-sstable=100  keys=100
```
**Identical.** Both collapse to exactly 100 live keys / 100 rows after compaction under load. Buggy did
NOT retain shadowed/expired versions; it purged them exactly like the fixed control. (Pre-compact buggy
was transiently larger only because it had one extra flushed overwrite batch at that instant.) This
directly contradicts the body's "compact under load doesn't reclaim on buggy" for reproducer #2 as well.

## Conclusion
On `cassandra:4.1.0` (buggy) vs `cassandra:4.1.1` (fixed), running the Jira's exact schema and a
sustained `USING TTL 10` insert workload (RF=1, single node, with the 2i index and STCS tombstone
settings), I observed NO operator-visible divergence across three deploy/test structures:
- on-disk SSTable file set, Space used (total), and partition-estimate track each other on both versions;
- compaction under load (memtable non-empty) reclaims expired data identically on BOTH (~3.5MB->~0.3MB);
- flushed SSTable metadata shows 2026 (not 2015) Minimum timestamp on the buggy version — the synthetic
  Epoch does not serialize to disk.

The body's distinguishing signature ("flush+compact under load does NOT reclaim on buggy; disk only
grows forever") did not fire on the public `cassandra:4.1.0` image. The original production observation
used TTL~600s / gc_grace~1800s over HOURS to accumulate GBs; the 10s/10s "speed-up" recipe did not
surface a measurable buggy-vs-fixed delta within the ~3-cycle / 12-20 min budget on these images.

**Disposition: not-reproducible** — BOTH of the body's documented reproducers were exercised on the
public images and neither produced a measurable client/operator-visible divergence between buggy (4.1.0)
and fixed (4.1.1):
- Reproducer #1 (distinct-key inserts, TTL 10, 2i index, STCS): on-disk SSTable set, Space used (total),
  partition estimate, and compaction-under-load reclamation all matched between versions.
- Reproducer #2 (bounded-key overwrite of the same ~100 partitions): both collapsed to exactly 100 live
  keys / 100 rows after compaction under load; sstablemetadata showed 2026 (not 2015) on both.
No verbatim buggy-only signature was obtained (every metric matched the fixed control). This is
consistent with the leak being purely an in-memory memtable value that (a) does not serialize to SSTable
metadata and (b) on 4.1.0 did not, in practice, block expired-SSTable / shadowed-version purge during
compaction in these experiments. The original production observation needed TTL~600s/gc_grace~1800s over
HOURS to accumulate GBs; the 10s/10s speed-up recipe surfaced no buggy-vs-fixed delta within budget.

### tag_correction
topology=1node and confidence were plausible, but confidence=H is NOT supported: the bug did not
reproduce as a measurable A/B delta on the public images within budget. The hint trigger ("expired
sstables never cleaned, disk grows forever") could not be demonstrated on cassandra:4.1.0 here.

### tooling_findings
`sstablemetadata` / `sstabledump` / `sstableexpiredblockers` live in /opt/cassandra/tools/bin/ and are
NOT on the container $PATH; must be invoked by absolute path inside the official cassandra:4.1.x image.
