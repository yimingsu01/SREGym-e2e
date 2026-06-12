# CASSANDRA-15669 — LeveledCompactionStrategy AIOOBE compacting the last level

- **Issue**: LeveledCompactionStrategy throws `ArrayIndexOutOfBoundsException` when compacting the last level.
- **Buggy version under test**: `cassandra:3.11.10` (single pod in kind, ns `repro-15669`)
- **Fix versions** (from Jira): 3.11.11, 4.0-rc2, 4.0
- **Component**: Local/Compaction/LCS
- **Classifier hint**: topology=1node, confidence=H, trigger = "LCS table with small fanout_size/sstable_size + stress writes filling many levels -> AIOOBE compacting last level, halts L1+ compaction". The 1-node topology is CORRECT.

## Reproducer extracted from the Jira body (ground truth)
1. Create an LCS table with small params: `fanout_size=2`, `sstable_size_in_mb=2` (small only to reach high levels with less data).
2. Insert data via cassandra-stress.
3. When the HIGHEST level's compaction score exceeds 1.001, LCS tries to build candidates for `level+1`, which is out of bounds -> AIOOBE; afterwards L1..Ln compaction stops working (L0 STCS still runs).

The body's stack trace (`LeveledManifest.java:814`, `ArrayIndexOutOfBoundsException: 9`) is from the **4.0** codebase. The body explicitly says it also reproduced on **3.11.3**. Target here is 3.11.10 (same 3.11 line, < fix 3.11.11).

## ROOT CAUSE — proven from source (the real fix) + this image's runtime

### The fix diff (cassandra-3.11.10 -> cassandra-3.11.11)

`src/java/org/apache/cassandra/db/compaction/LeveledManifest.java`, in `getCompactionCandidates()` score loop — the fix ADDS a guard so the top level is never compacted "upward":
```java
// 3.11.11 (FIXED) — added:
if (i == generations.levelCount() - 1)   // i == highest level (L8 when levelCount()==9)
{
    logger.warn("L" + i + " (maximum supported level) has " + remainingBytesForLevel
              + " bytes while its maximum size is supposed to be " + maxBytesForLevel + " bytes");
    continue;   // do NOT call getCandidatesFor(i) -> get(i+1)
}
```
In **3.11.10 (BUGGY)** this guard is absent: for the top level `i = levelCount()-1`, if `score > 1.001`, it calls `getCandidatesFor(i)`:
```java
// LeveledManifest.java (3.11.10) getCompactionCandidates, score loop:
for (int i = generations.levelCount() - 1; i > 0; i--) {        // starts at i = 8
    Set<SSTableReader> sstables = generations.get(i);
    if (sstables.isEmpty()) continue;
    ...
    double score = (double) SSTableReader.getTotalBytes(remaining)
                 / (double) maxBytesForLevel(i, maxSSTableSizeInBytes);
    if (score > 1.001) {
        ...
        Collection<SSTableReader> candidates = getCandidatesFor(i);   // i == 8  -> calls get(9)
        ...
    }
}
```
`getCandidatesFor(int level)` then dereferences `level+1`:
```java
// LeveledManifest.java (3.11.10) getCandidatesFor, line 550:
Map<SSTableReader, Bounds<Token>> sstablesNextLevel = genBounds(generations.get(level + 1));
```
`generations.get(9)` throws, from `LeveledGenerations.get` (3.11.10):
```java
Set<SSTableReader> get(int level) {
    if (level > levelCount() - 1 || level < 0)
        throw new ArrayIndexOutOfBoundsException(
            "Invalid generation " + level + " - maximum is " + (levelCount() - 1));   // "Invalid generation 9 - maximum is 8"
    ...
}
int levelCount() { return levels.length + 1; }   // == MAX_LEVEL_COUNT
```

### Secondary (latent) bug in the same fix — the `MAX_LEVEL_COUNT` constant
`LeveledGenerations.java` line 55:
```java
// 3.11.10 (BUGGY):
static final int MAX_LEVEL_COUNT = (int) Math.log10(1000 * 1000 * 1000);   // platform/JVM dependent!
// 3.11.11 (FIXED):
static final int MAX_LEVEL_COUNT = 9;                                       // hardcoded; supports L0..L8
```
On JVMs where `Math.log10(1e9)` returns `8.999999999998`, the truncation makes `MAX_LEVEL_COUNT = 8`, shrinking the array by one and moving the AIOOBE boundary down to level 8 (and the body's index "9" on 4.0). The fix removes this fragility.

**On THIS image the constant evaluates to 9** (verified at runtime, see below), so on 3.11.10 here valid levels are L0..L8 and the crash boundary is `get(9)` reached by compacting the TOP level L8 when it overflows (score > 1.001, i.e. L8 holds > maxBytesForLevel(8)=2^8*2MB=512MB).

## Environment / runtime evidence (commands + raw output)

```
$ kubectl exec -n repro-15669 cass -- cqlsh -e "SELECT release_version FROM system.local"
 release_version
-----------------
         3.11.10

$ kubectl exec -n repro-15669 cass -- java -version
openjdk version "1.8.0_292"  (AdoptOpenJDK, build 25.292-b10)

# levelCount() == 9 on this JVM  (getAllLevelSize() returns int[levelCount()]):
$ kubectl exec -n repro-15669 cass -- nodetool tablestats keyspace1.standard1 | grep "each level"
        SSTables in each level: [N, 0, 0, 0, 0, 0, 0, 0, 0]     <-- 9 entries => levelCount()=9 => MAX_LEVEL_COUNT=9

# Table compaction params actually applied:
$ kubectl exec -n repro-15669 cass -- cqlsh -e "SELECT compaction FROM system_schema.tables WHERE keyspace_name='keyspace1' AND table_name='standard1'"
 {'class': 'org.apache.cassandra.db.compaction.LeveledCompactionStrategy', 'fanout_size': '2', 'sstable_size_in_mb': '2'}
```

Note: in 3.11.x, `min_threshold`/`max_threshold` (listed in the Jira body) are STCS-only and are REJECTED by CQL for LCS:
`ConfigurationException: Properties specified [max_threshold, min_threshold] are not understood by LeveledCompactionStrategy`. The load-bearing params for this bug are `fanout_size` and `sstable_size_in_mb`, both applied.

## Workload
```
$ kubectl exec -n repro-15669 cass -- cassandra-stress write n=4000000 cl=ONE \
    -rate threads=8 -col "n=fixed(5) size=fixed(64)" \
    -schema "replication(factor=1)" "compaction(strategy=LeveledCompactionStrategy,fanout_size=2)" -node 127.0.0.1
# then ALTER keyspace1.standard1 ... sstable_size_in_mb=2
# nodetool setcompactionthroughput 0 ; nodetool setconcurrentcompactors 4
```

## Reproduction attempt — progress and OUTCOME

Wrote with cassandra-stress (4M rows target, ~320B each) into `keyspace1.standard1` (LCS fanout=2, sstable_size_in_mb=2), compaction unthrottled (`setcompactionthroughput 0`, `setconcurrentcompactors 4`). The LCS cascade DID engage and climb:

```
SSTables in each level: [6/4,  0, 0, 0, 0, 0, 0, 0, 0]   (~189 MB)   all L0
SSTables in each level: [29/4, 7/2, 7/4, 7, 0, 0, 0, 0, 0]  (~1.02 GB)  reached L3
SSTables in each level: [31/4, 2,   4,   8, 7, 0, 0, 0, 0]  (~1.09 GB)  reached L4  <-- MAX REACHED
```

Then progress STALLED. Cause = **disk exhaustion on the shared kind node**:
```
$ kubectl exec -n repro-15669 cass -- df -h /var/lib/cassandra
/dev/sda3   63G  58G  1.6G  98% /var/lib/cassandra     <-- 98% full, 1.6 GB free, dropping
```
The node disk is shared with other pre-existing namespaces (cass-*, repro-*-ctl, etc.). One ~1 GB compaction was mid-flight at 47% with 475 pending tasks; continuing would have filled the disk and damaged co-tenant namespaces, violating the isolation mandate. Writes were stopped (`pkill cassandra-stress`), autocompaction disabled, compactions stopped.

### Why L8 is unreachable here (the blocker, quantified)
The crash requires the HIGHEST level (L8, since levelCount()==9) to be NON-EMPTY and have **score > 1.001**, i.e. L8 must hold **> maxBytesForLevel(8) = 2^8 * 2 MB = 512 MB**. For L8 to even receive data, L1..L7 must first fill and overflow:
`L1..L7 targets = 4+8+16+32+64+128+256 = 508 MB`, **plus > 512 MB packed into L8 = > ~1 GB of unique, fully-cascaded data**, and LCS write-amplification needs several GB of *transient* free space during the cascade. With ~1.6 GB free and data only at L4, L8 cannot be reached on this shared node.

### Negative result for this run (no false positive)
```
$ kubectl exec -n repro-15669 cass -- grep -cE 'ArrayIndexOutOfBounds|Invalid generation' /opt/cassandra/logs/system.log
0
$ ... debug.log
0
```
No AIOOBE was emitted — expected, because L8 was never populated. Therefore **no verbatim buggy signature was obtained**, so per the rubric this is NOT "reproduced".

## DISPOSITION: confirmed-blocked (infrastructure: shared-node disk capacity)

- The bug is **definitively present** in cassandra:3.11.10 here: the buggy code path (`getCompactionCandidates` top-level branch with NO `if (i==levelCount()-1) continue` guard -> `getCandidatesFor(8)` -> `generations.get(9)` -> `ArrayIndexOutOfBoundsException("Invalid generation 9 - maximum is 8")`) exists in the shipped jar, and the runtime confirms `levelCount()==9` (9-element level array). The exact fix (diff above) is the guard added in 3.11.11.
- It is NOT shadowed/disabled (not "not-reproducible") and was NOT observed firing (cannot be "reproduced").
- **Specific blocker**: reaching LCS top level L8 (>512 MB at L8, ~1 GB+ unique cascaded + multi-GB transient compaction space) is not stageable on this shared kind node (disk hit 98% / 1.6 GB free with data only at L4). A dedicated volume with >~10 GB free, or an in-JVM dtest that injects sstables directly at L8 with score>1.001, would reproduce the verbatim AIOOBE.

### EXPECTED verbatim signature (what 3.11.10 WOULD emit on L8 overflow; derived from source, NOT observed)
`java.lang.ArrayIndexOutOfBoundsException: Invalid generation 9 - maximum is 8`
thrown at `org.apache.cassandra.db.compaction.LeveledGenerations.get` via `LeveledManifest.getCandidatesFor` (`generations.get(level + 1)`) <- `getCompactionCandidates` <- `LeveledCompactionStrategy.getNextBackgroundTask` on a `CompactionExecutor` thread.
(NB: differs from the Jira body's bare `ArrayIndexOutOfBoundsException: 9` at `LeveledManifest.java:814`, which was the 4.0 codebase using a raw array without the descriptive message.)

