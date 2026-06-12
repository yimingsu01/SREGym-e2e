# CASSANDRA-17342 — Reproduction Evidence Log

## Bug summary (primary source: /tmp/jira_repro/CASSANDRA-17342.json)
- **Summary:** Performance problem for node restart with incremental range repairs
- **Buggy version:** 4.0.2  | **fixVersions:** 4.0.3, 4.1-alpha1, 4.1
- **Components:** Consistency/Repair

### Body / mechanism (ground truth)
Clusters doing **incremental repairs with range repairs**. Reporter has 16 vnodes/node, splits
each vnode into 100 ranges, 22 keyspaces, RF=3 => ~**8100 records in `system.repairs`**. On node
restart, processing those records takes **30+ minutes** (even 10 ranges/vnode = 2 minutes).
Root cause: `org.apache.cassandra.repair.consistent.RepairState.add()` re-processes the complete
list **including a sort** on every Range add => super-linear (exponential) growth. Fix collects the
rows read from `system.repairs` in `LocalSessions` and processes them as a group at the end.
Reporter: *"this is demonstrated in the attached unit test."*

## Exact reproducer extracted
Operator-visible symptom = **node startup wall-time** grows super-linearly with the number of
`system.repairs` rows. To observe it you must FIRST populate `system.repairs` with thousands of
incremental-repair session records, THEN restart the node and time the boot.

## Disposition: needs-fix-test  (precise reasoning below)

### Finding 1 — There is NO client/server error signature. The effect is pure latency.
The bug manifests only as a slow startup; it throws no exception, logs no ERROR/WARN, and returns
no wrong result. The SREGym `reproduced` bar REQUIRES a verbatim exception / error text / wrong
result row. A slow boot produces none, so `reproduced` is structurally unclaimable for this bug
regardless of budget.

Startup log on the buggy 4.0.2 pod (fresh node, empty table) shows the repairs table is processed
**silently**:
```
INFO  [main] 2026-06-12 09:46:46,355 ColumnFamilyStore.java:385 - Initializing system.repairs
INFO  [MutationStage-1] 2026-06-12 09:48:15,209 ColumnFamilyStore.java:2252 - Truncating system.repairs
INFO  [MutationStage-1] 2026-06-12 09:48:15,209 ColumnFamilyStore.java:878 - Enqueuing flush of repairs: 0.469KiB (0%) on-heap, 0.000KiB (0%) off-heap
INFO  [PerDiskMemtableFlushWriter_0:2] ... Writing Memtable-repairs@... (0.048KiB serialized bytes, 1 ops, 0%/0% of on/off-heap limit)
INFO  [MutationStage-1] 2026-06-12 09:48:17,714 ColumnFamilyStore.java:2316 - Truncate of system.repairs is complete
```
`grep -iE "exception|error|WARN.*repair"` over the full startup log => NO repair-related
error/exception. Confirms: silent latency, not an error path.

### Finding 2 — Hand-seeding system.repairs does NOT reach the buggy code path.
`system.repairs` is normally populated by the incremental-repair coordinator (LocalSessions),
not by clients. Verified on the buggy pod:

Schema (note `ranges set<blob>` — the per-vnode split ranges the reporter inflates to 100x; these
serialized `Range<Token>` blobs are what `RepairState.add()` sorts on every insert):
```
CREATE TABLE system.repairs (
    parent_id timeuuid PRIMARY KEY,
    coordinator inet, coordinator_port int,
    last_update timestamp, repaired_at timestamp, started_at timestamp,
    state int, cfids set<uuid>, participants set<inet>,
    participants_wp set<text>, ranges set<blob>
) WITH ... comment = 'repairs' ...;
```
CQL writes to `system.repairs` ARE accepted and DO persist (clean re-test, no truncate between):
```
$ cqlsh -e "SELECT count(*) FROM system.repairs"      ->  count 0   (fresh node)
$ cqlsh -e "INSERT INTO system.repairs (parent_id, state) VALUES (now(), 1)"   (no error)
$ cqlsh -e "SELECT count(*) FROM system.repairs"      ->  count 1   (PERSISTS)
$ cqlsh -e "SELECT parent_id, state, ranges FROM system.repairs"
 parent_id                            | state | ranges
--------------------------------------+-------+--------
 5a30c170-6644-11f1-999c-5ff3658f788a |     1 |   null
```
But this does NOT help reproduce the bug, for two reasons:
1. **A minimal row has `ranges = null` (empty).** The buggy hot path is `RepairState.add()`
   re-sorting the *range list* on every range added. The trigger is the **total number of ranges
   across all sessions** (reporter: 16 vnodes x 100 = ~1600 ranges PER session), not the row count.
   A row with no ranges never exercises the O(n^2) sort.
2. **Synthesizing valid `ranges` blobs at scale is infeasible from cqlsh.** Each blob is a
   serialized `Range<Token>` in Cassandra's internal wire format; producing ~1600 correctly-encoded
   ranges per session x thousands of sessions by hand is out of budget and error-prone, and even
   then the per-row deserialization at startup (`LocalSessions.loadSessions`) must accept them.
   The realistic population path remains thousands of real `nodetool repair -inc` sessions on a
   live multi-node ring.

### Finding 3 — Producing the trigger volume is un-stageable in budget (and is CPU/time-bound,
NOT disk-bound).
Each `system.repairs` row = one incremental-repair session, and the cost scales with the RANGES
inside each session (~1600 ranges/session in the reporter's setup). The reporter's ~8100 rows came
from 22 keyspaces x RF3 x 16 vnodes x 100-way range splitting on a production cluster. Reaching
that requires hundreds of sequential `nodetool repair -inc` invocations on a live multi-node ring
(incremental repair requires anti-compaction across replicas). That cannot complete within the
12-20 min / ~3-cycle budget, and would still yield no qualifying signature.
The rows themselves are tiny (sub-KiB each), so this is NOT `blocked-disk-constrained` — it is
time/CPU-to-generate. The canonical, intended reproducer is the **JVM unit test attached to the
ticket** that drives `RepairState.add()` directly (reporter's own words). Running/adapting that
test needs a from-source build of Cassandra 4.0.2 (the prebuilt `cassandra:4.0.2` image ships no
test harness), which is out of budget here => **needs-fix-test**.

## A/B control (fixed image)
Fixed line ceiling for 4.0 is patch 20; fix shipped in **4.0.3** (`4.0.2 patch+1 = 4.0.3 <= 4.0.20`),
so `cassandra:4.0.3` exists and would be the A/B image. However A/B is **moot** here: with no error
signature and no feasible way to reach the trigger row-count, there is nothing to diff between
4.0.2 and 4.0.3 at any scale stageable in this environment. The fix is an internal startup-latency
optimization in `LocalSessions`/`RepairState`, observable only as wall-clock boot time at
production scale.

## Environment
- Cluster: kind-kind (4 nodes). Namespace created: `repro-17342`. Single pod `cass`, image
  `cassandra:4.0.2`, `num_tokens=16` (matches reporter's 16 vnodes).
- `cassandra -v` => `4.0.2` (confirmed buggy image).
- Host root disk ~82% used / ~11Gi free at start — another reason not to attempt bulk repair
  generation.

## tag_correction
Hint: topology=ring, trigger="many vnodes + range-split incremental repairs populate system.repairs
-> node restart takes 30+ min". The MECHANISM is accurate and real. But the actual reproducer is a
JVM unit test driving `RepairState.add()` (per reporter) OR production-scale generation of
thousands of incremental-repair sessions — NOT a symptom observable on a live ring within budget,
and it produces NO error/exception/wrong-result signature (pure startup latency). Single pod was
sufficient to establish the blocking facts; a ring was not needed.

## Verdict
**needs-fix-test** — canonical reproducer is the attached JVM unit test (needs source build,
out of budget). Even at production scale the only effect is startup wall-time with no verbatim
buggy signature, so `reproduced` is structurally unclaimable.
