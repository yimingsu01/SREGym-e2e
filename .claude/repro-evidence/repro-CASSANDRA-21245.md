# CASSANDRA-21245 reproduction log

**Bug:** "Uncompressed size is being used for compressed tables in maintenance operations"
**Buggy version:** 5.0.8 (storage-engine). Fix versions: 5.0.9, 6.0-alpha2, 7.0 (NO released fixed image -> within-version reasoning only).
**Topology:** single-node pod (reused dedicated pod `cass` in namespace `cass-21245`, stock `cassandra:5.0.8` with `max_space_usable_for_compactions_in_percentage: 0.0002` appended to cassandra.yaml).
**Disposition:** confirmed-blocked.

## Primary source (JIRA fields.description)
Reproducer per JIRA:
- CREATE compressed table `WITH compression = {'class':'DeflateCompressor'}`.
- Insert many highly-compressible rows (single char * 1024*1024 * N).
- Expect: table compacts fine. Observed (5.0.6 / 5.0.7): background compactions denied with
  `RuntimeException: Not enough space for compaction ... expected write size = <small, compressed>`
  AND `Directories.java:553 - FileStore ... has only 33.73 GiB available, but 89.39 GiB is needed`
  while `df -h` shows 42G free.
- Reporter's ring under HEAVY sustained load: `nodetool compactionstats` showed 31 pending tasks,
  5009 compactions completed, 1.1 TiB compacted, an active 136 GiB compaction. Tested buggy on 5.0.6/5.0.7;
  "4.1.11 works fine".

Root cause (per task + JIRA): the available-disk-space check in
`CompactionTask.buildCompactionCandidatesForAvailableDiskSpace` compares
`ColumnFamilyStore.getExpectedCompactedFileSize(...)` against
`Directories.getAvailableSpaceForCompactions = (usableSpace - min_free_space_per_drive) * max_space_usable_for_compactions_in_percentage`.
The JIRA contradiction is internal: the RuntimeException prints a SMALL "expected write size" (e.g. 2,195,528 = ~2 MB, compressed)
yet Directories simultaneously reports "89.39 GiB is needed" (uncompressed). The 89 GiB uncompressed figure only arises when the
expected-size estimate is summed across a LARGE STCS bucket of many big sstables under sustained compaction load.

## Lever verification (the 0.0002 lever IS active)
- `cassandra -v` => 5.0.8. Config dump (system.log) confirms loaded values:
  `max_space_usable_for_compactions_in_percentage=2.0E-4`, `min_free_space_per_drive=50MiB`.
- Data dir: `/dev/sda3` on `/var/lib/cassandra`, ~34,085,474,304 bytes (~34 GB) usable (shared kind worker disk).
- Computed lever = (34.0e9 - 50MiB) * 0.0002 ~= 6.8 MB.
- CONFIRMED empirically by DEBUG log: `Directories.java:550 - FileStore /var/lib/cassandra (/dev/sda3) has 6561796 bytes available, ...`
  (i.e. ~6.56 MB lever active at compaction time). This proves the 0.0002 lever took effect (default lever would be ~6.8 GB).

## Workload built (repro21245.bulk_data, now dropped)
- `CREATE TABLE repro21245.bulk_data (pk bigint PRIMARY KEY, data text)
   WITH compression = {'class':'DeflateCompressor'}
   AND compaction = {'class':'SizeTieredCompactionStrategy','enabled':'false'};`
  (DeflateCompressor + STCS autocompaction OFF confirmed via DESCRIBE.)
- Loaded 20 rows of `'z'*1048576` (~20 MiB uncompressed) via individual INSERTs, `nodetool flush` -> sstable #1.
  Repeated 20 more rows, flush -> sstable #2.
- Per-sstable uncompressed size from system.log: `Completed flushing ...nb-1-big-Data.db (20.001MiB)`,
  `...nb-2-big-Data.db (20.001MiB)` (each 20 MiB UNCOMPRESSED, ~3x the 6.56 MB lever).
- `nodetool tablestats`: SSTable count: 2; Space used (live): 245250 (~240 KiB ON DISK); SSTable Compression Ratio: 0.00510.
  => 40 MiB uncompressed total compresses to ~240 KiB on disk; the whole table fits trivially in the 6.56 MB lever.

## Tests run (all under the active ~6.56 MB lever) — ALL SUCCEEDED, none tripped the bug
Each maintenance op was run, rc captured, and the DEBUG `Directories.java:550 "checking if we can write N bytes"`
line inspected (N = the size the code actually passes to the space check):

1. `nodetool compact repro21245 bulk_data` -> rc=0. Log:
   `Compacting [nb-2-big, nb-1-big]` then `Compacted ... 208.937KiB to 109.034KiB (~52% of original)`.
   Space check: `has 6561796 bytes available, checking if we can write 213951 bytes`.
   => expected write size = 213,951 (~209 KiB) = COMPRESSED on-disk size, NOT uncompressed 40 MiB. Fits -> compaction proceeds.

2. `nodetool garbagecollect repro21245 bulk_data` -> rc=0. Space checks: `checking if we can write 111651 / 106975 bytes` (compressed).
3. `nodetool upgradesstables -a repro21245 bulk_data` -> rc=0. Space check: `checking if we can write 55818 bytes` (compressed).
4. `nodetool scrub repro21245 bulk_data` -> rc=0. (compressed-size checks, no trip)

In EVERY case the size passed to the disk-space check matched the COMPRESSED on-disk size (hundreds of KiB),
never the 20-40 MiB uncompressed size. NO `Not enough space`, NO `is needed`, NO RuntimeException was ever logged.
(Full debug.log scan with `grep -iE 'Not enough|is needed|expected write|checking if we can write'` -> only the compressed-size lines above.)

## Bytecode primary-source check (no javap/jar/network available; used python zipfile + constant-pool parse)
Extracted classes from `/opt/cassandra/lib/apache-cassandra-5.0.8.jar`:
- `CompactionTask.buildCompactionCandidatesForAvailableDiskSpace(Set,TimeUUID)Z` calls
  `ColumnFamilyStore.getExpectedCompactedFileSize(Iterable,OperationType)J` for the space decision.
- The `getCompressionRatio()D` / `getTotalCompressedSize()J` refs in CompactionTask are on
  `AbstractCompactionStrategy$ScannerList` (progress/throughput logging during the run), NOT the space decision.
- Comparison: 5.0.6 jar (JIRA says BUGGY) and 5.0.8 jar both reference the IDENTICAL set
  (`getExpectedCompactedFileSize`, `getCompressionRatio`, `getTotalCompressedSize`) in CompactionTask.
  => presence of compression-ratio refs does NOT distinguish buggy vs fixed; the bug is in the size MAGNITUDE
  summed across a large candidate set, not a missing ratio call in this path.

`getExpectedCompactedFileSize` returns `sum(sstable.onDiskLength) * survivingKeyRatio`. `onDiskLength` is the
COMPRESSED file length, which is exactly what my empirical 213,951 / 111,651 / 55,818-byte checks show for 5.0.8.

## Why this is confirmed-blocked (specific un-stageable mechanism)
The JIRA failure requires the expected-compacted-size estimate to balloon to TENS of GiB (89.39 GiB in the report)
so it exceeds available space. With a hand-built 2-sstable, 40-MiB-uncompressed / 240-KiB-on-disk table and
autocompaction OFF, the code computes the correct COMPRESSED candidate size (~200 KiB) and compaction/maintenance
SUCCEEDS even under a deliberately tiny 6.56 MB lever. To make the uncompressed-vs-compressed accounting diverge into
a denial I would need the reporter's actual production conditions: a LARGE multi-GiB STCS bucket of MANY big sstables
under SUSTAINED concurrent write + background-compaction load (their ring: 1.1 TiB compacted, 31 pending tasks,
136 GiB active compaction), where the summed estimate over the live candidate set crosses the available-space line.
That sustained-load / large-bucket / volume-and-timing window cannot be staged on a single small pod sharing a 34 GB
kind worker disk within the time/resource budget here. Lowering the lever does not help: the per-op check still uses
the compressed size, so it never trips for a small table regardless of how small the lever is (verified across 4 op types).

## Control (within-version reasoning; no 5.0.9 image exists)
The control is the juxtaposition captured above, on the SAME buggy 5.0.8 binary:
- On-disk compressed size = ~240 KiB (`nodetool tablestats` Space used live = 245250; ratio 0.00510).
- The size actually fed to the disk-space gate = 213,951 bytes (compressed), per the DEBUG line.
These match (compressed == checked size) and compaction is ALLOWED -> the uncompressed-accounting defect did NOT
manifest in any stageable single-node op. This is consistent with the task's "incompressible data the accounting
matches / compact succeeds once the lever is relaxed" control expectation: here the accounting already matched
(compressed) for every reproducer op I could stage, which is precisely why no denial occurred.

## Isolation / teardown
- Reused the pre-existing DEDICATED pod `cass` in namespace `cass-21245` (per task: dedicated to this bug, may reuse). NOT shared.
- Did NOT create namespace `repro-21245` (so nothing to tear down; `kubectl get ns repro-21245` => NotFound).
- Created keyspace `repro21245` inside cass-21245; DROPPED it after capture (`DROP KEYSPACE IF EXISTS repro21245` rc=0,
  verified 0 rows in system_schema.keyspaces). Removed /tmp/load1.cql,/tmp/load2.cql from the pod.
- No repo / SREGym / Cassandra files were edited (record-only).

## Verbatim most-telling line (the bug did NOT trip; this is the SUCCESS/compressed-size evidence, not a buggy signature)
`DEBUG [CompactionExecutor:7] 2026-06-11T21:26:25,254 Directories.java:550 - FileStore /var/lib/cassandra (/dev/sda3) has 6561796 bytes available, checking if we can write 213951 bytes`
(expected write size = 213,951 = COMPRESSED; well under the lever; compaction succeeded. No `Not enough space` / no `is needed` ever emitted.)
