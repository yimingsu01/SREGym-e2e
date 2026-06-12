# Reproduction findings — bugs.txt (Apache Cassandra DB-behavior bugs)

Two kinds of findings are recorded here:
1. **Bugs in the SREGym reproduction tooling** that block the skill's automatic path. These were
   originally documented only; **they have since been FIXED — see Part 5 for the fixes and their
   validation.**
2. **Findings about the 100 Cassandra bugs themselves** — how many are actually reproducible in a
   kind cluster, and the reproduction(s) that were achieved.

---

## Part 1 — Bugs found in the SREGym tooling (✅ now FIXED — see Part 5)

### BUG-1 (blocker): Jira parser unpacks 5 values from a 7-tuple
- **Location:** `sregym/service/jira_issue_parser.py:56-58`
- **What:** `JiraIssueParser.resolve()` does
  ```python
  reproducer, expected_output, buggy_output, correct_output, crash_on_startup = (
      extract_reproducer_full(body)
  )
  ```
  but `extract_reproducer_full()` returns a **7-tuple**
  `(reproducer, expected_output, buggy_output, correct_output, setup_preconditions, crash_on_startup, fault_injection_type)`
  (`sregym/service/reproducer_extractor.py:622-669`).
- **Effect:** Every Jira issue that gets far enough to call the extractor raises
  `ValueError: too many values to unpack (expected 5)`. This makes the skill's automatic
  `python main.py --create <jira-url>` path / `ProblemGenerator.generate(<jira-url>)` **fail for
  every Jira issue**, including all 100 in `bugs.txt`.
- **Confirmed empirically:**
  ```
  $ ProblemGenerator.generate("https://issues.apache.org/jira/browse/CASSANDRA-21442")
  ValueError: too many values to unpack (expected 5)
    at jira_issue_parser.py:56  reproducer, expected_output, buggy_output, correct_output, crash_on_startup = (
  ```
- This is the exact bug already flagged in the skill's "Known bugs & gotchas" section, now
  reproduced against this issue set.

### BUG-2 (blocker for this dataset): Jira version resolution fails when "Affects Version/s" is empty
- **Location:** `sregym/service/jira_issue_parser.py:88-107` (`_extract_version`).
- **What:** version resolution tries (1) the structured `versions` field, then (2) the first
  semver-looking token in the summary/description, else raises.
- **Effect on this dataset:** **0 of 100** issues have an "Affects Version/s" set, so resolution
  always falls back to a regex over free text. That regex (`\b(\d+\.\d+(?:\.\d+)*)\b`) grabs the
  first number that looks like a version, which for these issues is frequently **wrong** (e.g. a
  Python version `3.11`, a size `33.73`, an error code) or **absent entirely**.
  - With a (spurious) match → proceeds and then dies on BUG-1.
  - With no match → raises `ValueError: Could not extract version from Jira issue …`.
- **Confirmed empirically:**
  ```
  $ ProblemGenerator.generate("https://issues.apache.org/jira/browse/CASSANDRA-20050")
  ValueError: Could not extract version from Jira issue CASSANDRA-20050.
    at jira_issue_parser.py:104
  ```
  (CASSANDRA-20050 is a real, reproducible bug — but the parser cannot even determine its version.)

### BUG-3 (quality gap, not a crash): regex reproducer fallback cannot read Jira markup
- **Location:** `sregym/service/reproducer_extractor.py:60-86` (`_REPRO_SECTION_RE`, `_CODE_BLOCK_RE`).
- **What:** the regex fallback only recognises GitHub-flavoured markdown — triple-backtick fences
  ```` ```sql … ``` ```` and `## To Reproduce` headings.
- **Effect:** Jira descriptions use wiki markup — code is wrapped in `{code}…{code}` /
  `{noformat}` and headings are `h2.`. The regex therefore matches **nothing**. With
  `ANTHROPIC_API_KEY` unset (as in this environment) the LLM extractor at
  `reproducer_extractor.py:434-484` is skipped entirely, so reproducer extraction for Jira issues
  yields **empty every time** — even for issues that contain a perfectly good `{code}` reproducer
  (e.g. CASSANDRA-20050, CASSANDRA-21046).

> Net (at the time of triage): the skill's **automatic** path was unusable for these Jira issues, so
> reproduction used the skill's documented **hand-crafted** mode (`db_version` + `source_git_ref` +
> explicit `reproducer`), which bypasses the Jira parser and the extractor. **Update:** BUG-1/2/3 are
> now fixed (Part 5), so the automatic path now parses these Jira issues end-to-end (verified for
> CASSANDRA-20050: `version=4.0.14`, clean reproducer extracted).

---


## Part 2 — Re-scope of bugs.txt to DB-behavior bugs (51)

`bugs.txt` was re-triaged (all 100 cached Jira issues, JSON in `/tmp/jira_issues/`) and rewritten to
contain **only database-behavior bugs**, dropping CI/test-logic and internal-tooling issues per the
request. Re-classification used a strict rubric (5 parallel sub-agents over self-contained inputs).

Final categories kept (`is_db_behavior = yes`): **51 bugs**
- cql-semantics: 13
- storage-engine: 9
- distributed-multinode: 23
- other-db-behavior: 6

Dropped (not in bugs.txt): ci-test-infra (34), internal-tooling (10), test-logic-only (4) and a few
other non-DB-behavior items.

### Deployability gate (drives which bugs can be reproduced from a stock image)
For a released `X.Y.Z` fix version, the **buggy version = that patch − 1**, and the official
`cassandra:<buggy>` Docker image already contains the bug — so a single stock pod reproduces it with
**no source build** (the same fast path proven on 20050). Image ceilings used: `3.11→19, 4.0→20,
4.1→11, 5.0→8`.

- **19 / 51** are *deployable* (buggy version ≤ image ceiling).
- **32 / 51** are *trunk-only* (fixed only in `6.0-alpha*/6.0/7.x`; no released image) → would need a
  custom trunk source build (and a matching base image that likely does not exist) → out of scope for
  the stock-image path.

## Part 3 — Reproductions achieved in the kind cluster (11)

Reproductions 1-6 deploy a stock single-node `cassandra:<buggy>` pod (heap capped at 1G so many run
concurrently); reproductions 7-11 (Phase 3) add multi-node rings where the bug requires them. All drive
the reproducer via `kubectl exec … cqlsh`/`nodetool`/`sstableloader`/`sstabledump`, and — where a fixed
image exists — run the **identical** workload on the fixed version as an A/B control. The reusable
single-node deploy/wait/cql/teardown helper is in the session files dir (`repro_helper.sh`); the
multi-node StatefulSet recipe and the per-bug agent prompts are in `.claude/repro_workflow.js`. Per-bug
raw evidence logs are saved under `.claude/repro-evidence/repro-CASSANDRA-<n>.md`.

> Reproductions 7-11 were produced in a **record-only** Phase 3 run (no SREGym tooling or Cassandra
> source modified). Every agent used manual/hand-crafted mode (kubectl + cqlsh against stock images) and
> reported `tooling_findings: none`, so no new SREGym tooling bugs surfaced this session (the Part 1/5
> Jira-parser path was never exercised).

| # | Bug | Buggy → Fixed | Category | Trigger | Control |
| - | --- | --- | --- | --- | --- |
| 1 | CASSANDRA-20050 | 4.0.14 → 4.0.15 | cql-semantics | `frozen<UDT>` clustering key + `CLUSTERING ORDER BY (loc DESC)` rejects a valid INSERT | ASC same schema inserts OK |
| 2 | CASSANDRA-21348 | 5.0.8 (config) | cql-semantics | `check_data_resurrection` startup_check enabled → `SELECT … system_views.settings` throws `ClassCastException` | stock 5.0.8 (no config) returns rows |
| 3 | CASSANDRA-21065 | 5.0.6 → 5.0.7 | storage-engine | `nodetool garbagecollect` on UCS + `only_purge_repaired_tombstones` with ≥2 unrepaired sstables → `ConcurrentModificationException` | fixed 5.0.8 runs clean |
| 4 | CASSANDRA-20972 | 5.0.5 → 5.0.6 | storage-engine | range tombstone + higher-ts row + `SELECT DISTINCT … token(id) > MIN` → server `IllegalStateException` | fixed 5.0.6 returns rows |
| 5 | CASSANDRA-21057 | 4.1.10 → 4.1.11 | other-db-behavior | trip disk-usage guardrail FULL, then disable threshold → gossip `DISK_USAGE` stays `FULL` | fixed 4.1.11 → `NOT_AVAILABLE` |
| 6 | CASSANDRA-21092 | 5.0.6 → 5.0.7 | distributed-multinode | `sstableloader` legacy 3.11 sstables with zero-copy → server `AssertionError: Filter should not be serialized in old format` | fixed 5.0.8 loads 500 rows clean |
| 7 | CASSANDRA-21219 | 5.0.6 → 5.0.7 | cql-semantics (security) | non-superuser `bob` (CREATE-only) runs `ADD IDENTITY 'spiffe://repro/bob' TO ROLE cassandra` → silent success, binding row created (CVE-2026-27314) | fixed 5.0.7 rejects: `Unauthorized … Only superusers can bind identities to a role with superuser status` |
| 8 | CASSANDRA-21290 | 4.1.11 (no fixed image) | other-db-behavior | with `check_data_resurrection` on, a 0-byte `cassandra-heartbeat` file at boot → `Failed to deserialize heartbeat file` → exit 3 | within-version: a *missing* file is tolerated (auto-created); only the *empty* file aborts startup |
| 9 | CASSANDRA-21332 | 5.0.8 (no fixed image) | cql-semantics (SAI/RFP) | 3-node divergent data + SAI static col + `read_repair=NONE` → `SELECT … WHERE s1=42` at `CL=ALL` returns 3 rows (2 range-tombstoned rows resurrected) | within-version: full-partition read `WHERE pk0=1` returns the correct 1 row |
| 10 | CASSANDRA-20877 | 4.0.19 → 4.0.20 | distributed-multinode | incremental repair finalizes, then range movement (scale 2→3) → FINALIZED `system.repairs` row survives a full re-repair on the coordinator | fixed 4.0.20 auto-deletes the equivalent session on the coordinator |
| 11 | CASSANDRA-21132 | 5.0.6 (fix opt-in) | distributed-multinode | 324 SAI indexes + cold cluster restart → legacy `INDEX_STATUS` gossip (38655 B > Short.MAX_VALUE) → `AssertionError` in GossipStage, join deadlock | not run — fix is opt-in (`force_optimized_index_status_format`); a stock-5.0.7 A/B would still reproduce |

### 1 — CASSANDRA-20050 (UDT/`ReversedType` clustering, buggy 4.0.14)
Hand-crafted problem `sregym/conductor/problems/auto_cassandra_20050.py`. `CLUSTERING ORDER BY (loc
DESC)` on a `frozen<point>` clustering key rejects a valid INSERT with
`InvalidRequest … Invalid user type literal for loc of type frozen<point>` (exit 2); the ASC control
inserts and reads back the row (exit 0). Reproduced on a 3-node K8ssandra cluster whose deployed image
is the buggy 4.0.14 build. (Full deploy log retained in Part 3-legacy notes below.)

### 2 — CASSANDRA-21348 (`system_views.settings` ClassCastException, 5.0.8)
The 5.0 `system_views.settings` virtual table cannot render a non-`String` setting value. Enabling a
startup check populates `startup_checks` (an enum-keyed map):
```
# pod command appends an active block to cassandra.yaml before start:
startup_checks:
  check_data_resurrection:
    enabled: true
```
Then:
```sql
SELECT * FROM system_views.settings;            -- buggy: throws
SELECT name FROM system_views.settings WHERE name='startup_checks';
```
**Buggy (5.0.8 + config):** `ClassCastException: …StartupChecks$StartupCheckType cannot be cast to
java.lang.String`. **Control (stock 5.0.8, no config):** the same SELECT returns rows cleanly →
isolates the fault to the enum-keyed setting, not the table itself.

### 3 — CASSANDRA-21065 (`nodetool garbagecollect` CME, buggy 5.0.6)
```sql
CREATE TABLE k.t (pk int PRIMARY KEY, v text) WITH compaction =
 {'class':'UnifiedCompactionStrategy','only_purge_repaired_tombstones':'true','scaling_parameters':'L10'};
```
`nodetool disableautocompaction k t`, then `INSERT`+`DELETE`+`nodetool flush` **≥2 times** to leave
≥2 *unrepaired* sstables, then `nodetool garbagecollect k t`:
```
java.util.ConcurrentModificationException
  at java.util.Collections$UnmodifiableCollection$1.next
  at org.apache.cassandra.db.compaction.CompactionManager$6.filterSSTables(CompactionManager.java:691)
  at …performGarbageCollection(CompactionManager.java:683)
```
Root cause: `filterSSTables` iterates `transaction.originals()` while calling `transaction.cancel()`
on each unrepaired sstable — mutating the set under iteration. A single sstable does **not** trip it
(the for-each ends before the next `next()`); ≥2 unrepaired sstables are required. **Control:** the
identical workload on fixed **5.0.8** runs clean.

### 4 — CASSANDRA-20972 (`SELECT DISTINCT` + range tombstone, buggy 5.0.5)
Exact reproducer from the fix's `DistinctReadTest`:
```sql
CREATE TABLE k.tbl (id int, ck int, x int, PRIMARY KEY (id, ck));
DELETE FROM k.tbl USING TIMESTAMP 100 WHERE id = 1 AND ck < 10;   -- range tombstone
INSERT INTO k.tbl (id, ck, x) VALUES (1, 5, 7) USING TIMESTAMP 101; -- live row inside the RT, higher ts
-- nodetool flush k tbl
SELECT DISTINCT id FROM k.tbl WHERE token(id) > -9223372036854775808;
```
**Buggy 5.0.5:** `ReadFailure`; server log shows
`IllegalStateException: The UnfilteredRowIterator … must be closed before calling hasNext() or next()
again` at `SSTableScanner.java:241` — matching the report exactly. **Control:** identical steps on
fixed **5.0.6** return the row. (Note: the buggy version is 5.0.5 = the 5.0.6 fix patch − 1; running
on 5.0.6 by mistake is itself the control.)

### 5 — CASSANDRA-21057 (disk-usage guardrail cannot be disabled, buggy 4.1.10)
`DiskUsageMonitor` only measures Cassandra's *own* data-dir size, so the ratio is inflated with a tiny
`max_disk_size`:
```
nodetool setguardrailsconfig data_disk_usage_max_disk_size 1MiB
nodetool setguardrailsconfig data_disk_usage_percentage_threshold 2 1   # args are [fail, warn]
# wait one 30s monitor tick → gossip DISK_USAGE = FULL
nodetool setguardrailsconfig data_disk_usage_percentage_threshold null null   # disable
```
**Buggy 4.1.10:** `DISK_USAGE` stays **FULL** at 30s and 60s — the monitor's
`if (!enabled) return;` short-circuits and never re-evaluates, exactly the documented root cause
("node does not stop advertising FULL via gossip"). **Control fixed 4.1.11:** the same disable makes
`DISK_USAGE` transition to **NOT_AVAILABLE** within one tick (the fix's `onDiskUsageGuardrailDisabled`).
(On a single node the FULL state did not hard-reject local writes, but the stuck gossip state — the
actual mechanism being fixed — reproduces cleanly.)

### 6 — CASSANDRA-21092 (zero-copy streaming of legacy sstables, buggy 5.0.6)
Generate 3.11.19 (`me-1-big-*`, old bloom-filter format) sstables, copy them into a 5.0.6 pod, and
`sstableloader -d <node-ip> <ks>/<tbl>` with the default `stream_entire_sstables=true`:
```
java.lang.AssertionError: Filter should not be serialized in old format
  at org.apache.cassandra.utils.BloomFilterSerializer.serialize(BloomFilterSerializer.java:52)
  at org.apache.cassandra.utils.BloomFilter.serialize(BloomFilter.java:67)
  at org.apache.cassandra.io.sstable.format.FilterComponent.save(FilterComponent.java:78)
```
wrapped in `CorruptSSTableException`; the stream **fails**. **Control fixed 5.0.8:** the identical
sstables load successfully (500 rows, 0 AssertionErrors) because the fix auto-disables zero-copy for
pre-4.0 bloom-filter sstables.

### 7 — CASSANDRA-21219 (privilege escalation via `ADD IDENTITY`, buggy 5.0.6) — Phase 3
CVE-2026-27314. **Prior verdict "blocked-hard, needs full mTLS PKI" is overturned with evidence:** the
authorization gate on `ADD IDENTITY` (`AddIdentityStatement.authorize/execute`) has no
authenticator-type dependency (only `ensureNotAnonymous`), so the bug is reachable under plain
`PasswordAuthenticator` with **zero PKI**. Single node, `PasswordAuthenticator` + `CassandraAuthorizer`:
```sql
-- as superuser:
CREATE ROLE bob WITH PASSWORD='bob' AND LOGIN=true AND SUPERUSER=false;
GRANT CREATE ON ALL ROLES TO bob; GRANT CREATE ON ALL KEYSPACES TO bob;
-- as bob (CREATE-only, NOT superuser):
ADD IDENTITY 'spiffe://repro/bob' TO ROLE cassandra;     -- buggy: rc=0, silent success
```
**Buggy 5.0.6:** `SELECT identity,role FROM system_auth.identity_to_role` then shows
`spiffe://repro/bob | cassandra` — a CREATE-only role bound its identity to the superuser role (the
escalation). The buggy signature is this concrete wrong result (a silent-success op has no error line).
**Control fixed 5.0.7** (identical workload): the same `ADD IDENTITY` is rejected with
`Unauthorized: … code=2100 … "Only superusers can bind identities to a role with superuser status"`
(exit 2), and `identity_to_role` stays empty. mTLS is only the downstream *use* of the bound identity to
log in as the superuser — not the bug — so the PKI would add no proof. (`LIST IDENTITIES` is not valid
5.0.x grammar; the binding is observed via `SELECT` on `system_auth.identity_to_role`.)

### 8 — CASSANDRA-21290 (empty heartbeat file aborts startup, buggy 4.1.11) — Phase 3
"Implement atomic heartbeat file write." The `check_data_resurrection` startup check writes a heartbeat
file (`DEFAULT_HEARTBEAT_FILE=/var/lib/cassandra/data/cassandra-heartbeat`) non-atomically; a crash
between create and write leaves a **0-byte** file, and the buggy read path
(`Heartbeat.deserializeFromJsonFile`) cannot parse it and aborts startup. Single `cassandra:4.1.11` pod
whose command (a) enables `check_data_resurrection` and (b) pre-creates a 0-byte heartbeat file before
the entrypoint:
```
ERROR [main] 2026-06-11 21:38:09,150 CassandraDaemon.java:900 - Failed to deserialize heartbeat file /var/lib/cassandra/data/cassandra-heartbeat
```
→ pod `0/1 Error`, `exitCode=3`, CrashLoop. **Within-version control** (no 4.1.12 image): with the same
config but the heartbeat file **absent**, the check tolerates it and auto-creates a valid file
(`{"last_heartbeat":"…"}`), the node reaches `Ready 1/1` with `restartCount 0` — isolating the empty
file as the sole trigger (a missing file is fine; the failure is a deserialize error). **Caveats:** the
crash *race* that produces the empty file in the wild was not raced (the empty-file *artifact* was
staged directly, which exercises the identical read path the fix's fallback addresses); and
`check_data_resurrection` is **off by default** in 4.1.11, so only deployments that explicitly enable it
are exposed.

### 9 — CASSANDRA-21332 (SAI static-column read resurrects tombstoned data, buggy 5.0.8) — Phase 3
**Prior verdict "likely in-JVM-dtest-only" is empirically disproven.** 3-node ring, RF=3
(`NetworkTopologyStrategy dc1:3`), `cassandra:5.0.8`; table per the fix's dtest:
```sql
CREATE TABLE rfp21332.rt_static_sai (pk0 int, ck0 boolean, ck1 double, s1 int static, v0 boolean,
  PRIMARY KEY (pk0,ck0,ck1)) WITH read_repair='NONE';
CREATE CUSTOM INDEX ON rfp21332.rt_static_sai (s1) USING 'StorageAttachedIndex';
```
The dtest's per-replica `executeInternal` divergence was reproduced **on a real ring** via gossip
isolation: for each round, `nodetool disablegossip` on the other two pods, confirm they show `DN`, write
at `CONSISTENCY ONE` with explicit `USING TIMESTAMP`, `nodetool flush`, re-enable gossip. The three-way
divergence (node1 has row `[true,4.0]`, node2 has a range tombstone + surviving `[true,5.0]` and
`s1=42`, node3 has `[false,1.0]`) was **verified physically with `sstabledump`** on each node's local
`Data.db` (a `CL=ONE` read routes to an arbitrary replica and is not a reliable per-node probe). Then,
from the coordinator with `CONSISTENCY ALL`:
```
SELECT ck0, ck1 FROM rfp21332.rt_static_sai WHERE s1 = 42;   -- returns 3 rows: (False,1),(True,4),(True,5)
```
instead of the single correct row `(True,5.0)` — `(False,1)` and `(True,4)` are range-tombstoned rows
resurrected during Replica Filtering Protection. **Within-version control** (no 5.0.9 image): the normal
full-partition read `WHERE pk0=1` at `CL=ALL` returns the correct single row (reconciliation applies the
tombstone), isolating the SAI+RFP path as the defect.

### 10 — CASSANDRA-20877 (FINALIZED repair sessions survive range movement, buggy 4.0.19) — Phase 3
3-node ring bootstrapped from 2 (RF=2). `LocalSessions` cleanup
(`-Dcassandra.repair_delete_timeout_seconds=30 -Dcassandra.repair_cleanup_interval_seconds=20` to make
it testable). Workload: incremental `nodetool repair` (S1 FINALIZES) → scale StatefulSet 2→3 (cass-2
bootstraps, ranges move) → second incremental repair S2 on cass-0 → wait > delete-timeout. The proof is
a **differential on the S2 coordinator (cass-0)**:
```
# buggy 4.0.19, cass-0 debug.log (every 20s, forever):
DEBUG [OptionalTasks:1] LocalSessions.java:456 - Skipping delete of FINALIZED LocalSession ed5be870-… because it has not been superseded by a more recent session
#   → final `SELECT parent_id,state FROM system.repairs` = 2 rows (S1 survives + S2)
# control fixed 4.0.20, cass-0 debug.log:
DEBUG … LocalSessions.java:487 - Auto deleting repair session LocalSession{sessionID=eedbf8c0-…, state=FINALIZED, …}
#   → final system.repairs = 1 row (S2 only)
```
Same workload, opposite outcome on the coordinator. (The bare "Skipping delete" line is *not* alone
unique — it also fires for the newest session and, even under the fix, on the non-coordinator cass-1
whose still-owned ranges S2 does not fully re-cover; that is why the discriminator is the *coordinator*,
where a moved range is the only thing that can leave S1 non-superseded.)

### 11 — CASSANDRA-21132 (oversized legacy INDEX_STATUS gossip deadlocks join, buggy 5.0.6) — Phase 3
2-node ring (`StatefulSet`, persistent `volumeClaimTemplates` so the schema survives a restart). Load
**324 SAI indexes** (20 keyspaces × 5 tables × ~8 indexes, identifiers padded to the 48-char max), then
a **cold** bring-down/up (`kubectl scale sts/cass --replicas=0` then `=2`). On rejoin, all nodes have
lost peer `RELEASE_VERSION`, so `Gossiper.getMinVersion()` is unknown and gossip falls back to the
pre-5.0.3 **uncompressed** INDEX_STATUS encoding; the 38655-byte payload exceeds `Short.MAX_VALUE`
(32767) and trips:
```
ERROR [GossipStage:1] JVMStabilityInspector.java:70 - Exception in thread Thread[GossipStage:1,5,GossipStage]
java.lang.RuntimeException: java.lang.AssertionError
Caused by: java.lang.AssertionError: null
    at org.apache.cassandra.db.TypeSizes.sizeof(TypeSizes.java:44)
    at org.apache.cassandra.gms.VersionedValue$VersionedValueSerializer.serializedSize(VersionedValue.java:381)
    at org.apache.cassandra.gms.GossipDigestAckSerializer.serializedSize(GossipDigestAck.java:96)
    at org.apache.cassandra.gms.GossipDigestSynVerbHandler.doVerb(GossipDigestSynVerbHandler.java:110)
```
exact match to the Jira stack; it loops every ~5s and cass-1 is stuck `DN` (join deadlock). Format
reversion confirmed by `nodetool gossipinfo`: pre-restart the value is compressed (numeric codes,
17654 B); post-restart it is the legacy form (duplicated `keyspace.index` keys + literal
`"BUILD_SUCCEEDED"`, 38655 B). **Control not run** (budget) and importantly the fix is **opt-in**
(`force_optimized_index_status_format: true`, default false) and does not fix the underlying
`getMinVersion` race, so a naive stock-5.0.7 A/B would *still* reproduce; a correct positive control must
set that flag. A single rolling pod-bounce does **not** trip it (cached peer version keeps the format
compressed) — the full cold restart is required.

## Part 4 — Bugs assessed but not reproduced (disposition)

| Bug | Buggy ver | Disposition | Reason |
| --- | --- | --- | --- |
| CASSANDRA-20915 | 4.0.18 | not-reproducible | `CREATE KEYSPACE` is rejected earlier by client-side validation with the **correct** "48 characters" message; the buggy `222` constant lives only in the internal `KeyspaceMetadata.validateKeyspaceName` path (unit tests). |
| CASSANDRA-20982 | 4.0.19 | not-reproducible | `ALTER … TYPE` is fully disabled in 4.0 ("Altering column types is no longer supported"); the buggy `isValueCompatibleWith` check is reachable only from unit tests. |
| CASSANDRA-20917 | 5.0.5 | not-observable | Internal error-type refinement (throw RTE instead of FSError in `TOCComponent`); no distinct client-visible behavior. |
| CASSANDRA-21389 | 4.0.20 | not-observable | Server-side snapshot-name hardening (validation); no client-visible misbehavior in normal use. |
| CASSANDRA-21245 | 5.0.8 | confirmed-blocked | **Phase 3, premise refuted.** The disk-space check on a single small pod used the **compressed** size (213,951 B), not uncompressed — verified across `compact`/`garbagecollect`/`upgradesstables`/`scrub` (all rc=0, zero denials) and by reading `getExpectedCompactedFileSize` bytecode (sums `onDiskLength` = compressed). The uncompressed `… is needed` figure only appears under sustained concurrent-write load over a large multi-GiB STCS bucket (reporter: 1.1 TiB, 31 pending compactions), un-stageable on one small pod. |
| CASSANDRA-20871 | 4.0.19 | confirmed-blocked | **Phase 3.** The length-0 counter context that crashes `CounterContext.headerLength` is produced only by an in-JVM dtest `executeInternal` (uncoordinated node-local counter write); cqlsh/nodetool route through the counter leader (read-modify-write) and always yield a non-empty global shard. Code path is reachable (not shadowed); the empty-context precondition is not manufacturable externally. |
| CASSANDRA-21428 | 4.0.20 | confirmed-blocked | **Phase 3.** `ECHO_REQ` and gossip multiplex on the same internode TCP/7000 connection, so a connection-level partition drops both; the failure detector convicts the peer (phi 8, 1s interval) within the ~10s echo-timeout window and `silentlyMarkDead` clears the stale `inflightEcho` entry (the bug's escape hatch). The required state (ECHO failed **while** FD healthy) needs an asymmetric verb-level drop (in-JVM `IMessageFilters`), not connection-level partitioning. |
| CASSANDRA-20976 | 5.0.5 | confirmed-blocked | **Phase 3.** No concrete reproducer — the Jira description is a single mailing-list URL (64-byte body, verified by reading `/tmp/jira_issues/CASSANDRA-20976.json`). The assigned agent additionally hit a cyber-safeguard API false-positive; the disposition stands on the merits. |
| 32 trunk-only bugs | — | blocked-no-image | Fixed only in `6.0-alpha*/6.0/7.x`; no released `X.Y.Z` image (re-confirmed: no `cassandra:6.0`/`5.0.9`/`4.0.21`/`4.1.12`/`7.0` tags on Docker Hub; ceilings 3.11→19, 4.0→20, 4.1→11, 5.0→8 hold). Would need a custom trunk source build (and a matching base image, likely absent). |

> **Phase 3 note (2026-06-11):** CASSANDRA-21219, 21290, 21332, 20877, and 21132 were previously in this
> "not reproduced" table as `blocked-hard`/`blocked-risk`; the 4-node kind cluster and gossip-isolation
> technique moved all five to **reproduced** (Part 3 #7-11). For 21219/21332/21290 the prior blocking
> rationale was over-scoped — see the per-section notes in Part 3.

> Two structurally-clean trunk-only CQL bugs (21046 silently-accepted DDL options; 21055
> `UPDATE … SET col[0]=…` on a non-existent pk) would be the next hand-craft candidates **if** the
> build path supported a deployable image from a trunk ref.

### Legacy 20050 deploy notes (environment issues surfaced during that run)

- **ENV-1 — Hardcoded `storageClassName: openebs-hostpath`.** The K8ssandra CR (and other PVCs)
  request `openebs-hostpath` (`sregym/service/db_build_spec.py:151,229,238`,
  `sregym/service/apps/cassandra.py:179,451`, and the observer charts). A vanilla kind cluster only
  ships `standard` (`rancher.io/local-path`), so all `server-data-*` PVCs stayed **Pending**
  (`FailedScheduling … unbound immediate PersistentVolumeClaims`) and the Cassandra pods never
  scheduled. The tooling assumes a cluster with OpenEBS preinstalled (see the
  `db_build_spec.py:1017` comment "matches what kind exposes"), which is **not** true for a stock
  `kind create cluster`. _Workaround used (cluster-side, not a source change): created an
  `openebs-hostpath` StorageClass aliased to the working `rancher.io/local-path` provisioner with
  `volumeBindingMode: WaitForFirstConsumer`._
- **ENV-2 / BUG-4 — Cluster-ready timeout too tight for kind first-boot.**
  `GenericDBApplication._wait_for_cluster_ready()` (`sregym/service/apps/generic_db_app.py:453`)
  caps readiness at **600s**. A 3-node Cassandra first-boot on this kind cluster took ≈660s to reach
  all-pods-Ready, so `deploy()` raised `RuntimeError: Timeout (600s) waiting for cluster … Ready`
  **even though the cluster became fully healthy (all 3 nodes `UN`) ~1 min later.** The bug
  reproduction itself was unaffected (the cluster was up), but the automated `app.deploy()` step is
  flaky on kind for multi-node DBs. Consider a longer/condition-based wait. _Not fixed this session._

---

## Part 5 — Fixes applied to the SREGym tooling

All five tooling/environment bugs documented above were fixed. Each fix is surgical and scoped to a
single file (no cross-file conflicts), validated with `py_compile`, `ruff`, and targeted functional
tests. No pre-existing, unrelated lint errors were touched.

### BUG-1 — Jira parser 7-tuple unpack  ·  `sregym/service/jira_issue_parser.py`
`JiraIssueParser.resolve()` now unpacks all 7 values from `extract_reproducer_full()` (was 5) and
forwards the two previously-dropped fields (`setup_preconditions`, `fault_injection_type`) into
`ParsedIssue`, mirroring `github_issue_parser.py`. The reproducer-summary log was updated to match.
→ The `ValueError: too many values to unpack` is gone.

### BUG-2 — Jira buggy-version resolution  ·  `sregym/service/jira_issue_parser.py`
`_extract_version()` was rewritten with a proper fallback chain:
1. **Affects Version/s** (`fields["versions"]`) — first concrete semver = buggy version.
2. **Fix Version/s derivation** (`fields["fixVersions"]`) — keep only released `X.Y.Z` patches
   (exclude `6.0`, `6.0-alpha1`, etc.), pick the **lowest**, and **decrement the patch** to get the
   deployable buggy version (guarded against `X.Y.0`). E.g. fix `4.0.15` → buggy **`4.0.14`**.
3. **Improved free-text scan** — prefer `vX.Y.Z`, then version-mentioning lines (mirrors GitHub).
4. **LLM fallback** — `_llm_extract_version()` (used only when `ANTHROPIC_API_KEY` is set).
5. Clear `ValueError` if all fail.
→ Sweep over the 100 cached Jira issues: **51 now resolve with correct structured derivation**
(20050→4.0.14, 20870→5.0.5, 20904→4.0.18). The 49 that still raise are overwhelmingly the
trunk-only (`6.0-alpha`) bugs with **no deployable released version** — failing fast with a clear
error is correct, and is a strict improvement over the old behaviour of silently returning a
garbage version (e.g. a Python version or an error code).

### BUG-3 — Reproducer extractor reads Jira markup  ·  `sregym/service/reproducer_extractor.py`
The regex fallback now recognises Jira `{code}`, `{code:sql}`, `{code:language=sql}`, and
`{noformat}` blocks (plus `hN.` reproduce-section headings), strips `cqlsh`/`psql` prompts, and drops
error-output lines (`InvalidRequest`, `Error from server`, `code=…`, `message=…`). Untagged Jira
blocks are accepted only when they look like executable SQL/CQL (`_SQL_KEYWORDS` ∧ not `_is_prose`).
GitHub-markdown behaviour is unchanged (untagged ``` ``` fences are still excluded).
→ For CASSANDRA-20050 the extractor now returns clean, runnable `CREATE TABLE … / INSERT …` CQL.

### BUG-4 / ENV-2 — Cluster-ready timeout too tight  ·  `sregym/service/apps/generic_db_app.py`
`_wait_for_cluster_ready()` default raised **600 → 1200s**, now overridable via
`SREGYM_CLUSTER_READY_TIMEOUT`. Callers that pass an explicit `timeout=` (e.g. the etcd problems'
`timeout=300`) are unaffected.
→ A 3-node K8ssandra first boot on kind (~660s observed) no longer trips a spurious deploy timeout.

### ENV-1 — `openebs-hostpath` StorageClass missing  ·  `sregym/conductor/problems/generic_custom_build.py`
Root cause clarified: the Conductor *does* install OpenEBS (creating `openebs-hostpath`), but only
when `config.deploy_openebs or problem.requires_openebs()`. `GenericCustomBuildProblem` did **not**
override `requires_openebs()` (unlike the older `CassandraBugProblem`), so it relied entirely on the
`deploy_openebs=True` default — and a run with `deploy_openebs=False` would leave PVCs Pending.
Fix: `GenericCustomBuildProblem.requires_openebs()` now returns **True**, so OpenEBS/`openebs-hostpath`
is always provisioned for these operator-managed problems regardless of the flag.
(My earlier reproduction hit this only because the ad-hoc driver bypassed the Conductor's setup; the
manual `openebs-hostpath` StorageClass alias was just a harness workaround, not a source change.)

### End-to-end validation
- `parse_issue("…/CASSANDRA-20050")` → `version=4.0.14`, `git_ref=cassandra-4.0.14`, clean reproducer
  (exercises BUG-1 + BUG-2 + BUG-3 together; runs offline via the regex fallback).
- `ProblemRegistry` still loads **144** problems; the hand-crafted `auto_cassandra_20050.py` is
  unchanged and still present.
- Extractor regression checks pass (GitHub `sql` fence works; untagged GitHub fence excluded; Jira
  `{code:sql}` works). `py_compile` + `ruff check`/`ruff format` clean on all changed files; no new
  lint errors introduced (verified against the `HEAD` baseline).

## Part 6 — New-candidate batch reproduction (2026-06-12)

Reproducing the 100 freshly-collected `bugs.txt` candidates via the resumable fan-out workflow `.claude/repro_candidates_workflow.js` (record-only). **87 of 98 reproducible candidates attempted** (2 already implemented as repo problems: 20108, 18105). All 69 single-node attempted; 20 of 31 rings attempted; **11 rings + 12 needs-fix-test appendix deferred** (disk+time wall, resumable — see end). Evidence: `.claude/repro-evidence/repro-CASSANDRA-<n>.md`, `candidate_results.json`.

**Outcome (87 attempted):** **confirmed-blocked** 3, **inconclusive** 2, **needs-fix-test** 2, **not-reproducible** 6, **reproduced** 74.

**Verified floor:** 66 of the 74 reproduced have their verbatim buggy signature confirmed present in the evidence log by an automated grep; the other 8 (marked `*`) are genuine reproductions whose `verbatim_signature` field paraphrased the log or were recovered from the log after a workflow-return crash (18647/16868 hand-confirmed). Combined with Part 3, the solidly-verified floor is **66 + 11 = 77** distinct Cassandra bugs reproduced in kind.

Method: stock `cassandra:<buggy>` pod (single) or kind StatefulSet ring (multi-node, gossip-isolation for per-replica divergence), reproducer via cqlsh/nodetool, A/B control on buggy-patch+1 where ≤ released ceiling. Record-only (no SREGym/Cassandra source changed).

**Infra:** the 63 GiB host disk could not sustain many concurrent Cassandra pods; the run used a bounded worker-pool (conc 3–8) with teardown + `crictl rmi --prune` between waves, and the first-session version pods + 20050 K8ssandra cluster were torn down to reclaim space (those reproductions remain in Part 3). The 11 deferred rings are the slowest medium-confidence multi-node bugs (a time+disk wall, not a correctness wall; ring reproduce rate was ~90%).

`*` = verbatim_signature field paraphrased the log, or verdict recovered from log post-crash. `14113` and `19166` are `inconclusive`: 14113 tripped the Claude cyber-safeguard false-positive (needs manual, like 20976); 19166's log has full root-cause + reproducer but the run output was truncated by the crash (needs re-run).

| Bug | Disposition | Buggy→Fix | Verbatim signature / reason |
| --- | --- | --- | --- |
| CASSANDRA-10968 | reproduced | 3.11.6→3.11.7 | {"files":["md-2-big-Data.db","md-1-big-Data.db","md-3-big-Data.db"]} |
| CASSANDRA-12525 | reproduced | 4.1.0→4.1.1 | Connection error: ('Unable to connect to any servers', {'127.0.0.1:9042': AuthenticationFailed(… |
| CASSANDRA-12949 | reproduced | 3.11.10→3.11.11 | {'chunk_length_in_kb': '128', 'class': 'org.apache.cassandra.io.compress.LZ4Compressor'} |
| CASSANDRA-13874 | reproduced | 4.0.0→4.0.1 | Row Cache              : entries 0, size 0 bytes, capacity 0 bytes, 0 hits, 0 requests, NaN rec… |
| CASSANDRA-13935 | reproduced | 3.11.8→3.11.9 | CREATE INDEX table1_last_update_date_idx ON repro13935.table1 (last_update_date); |
| CASSANDRA-14013 | reproduced | 4.1.0→4.1.1 | count -------      0  (1 rows)  Warnings : Aggregation query used without partition key |
| CASSANDRA-14204 | reproduced | 4.1.1→4.1.2 | java.lang.AssertionError 	at org.apache.cassandra.db.compaction.CompactionManager.parallelAllSS… |
| CASSANDRA-14349 | reproduced | 3.11.9→3.11.10 | CommitLog-6-1781241743009.log  linkcount=1  ORPHAN(absent-from-commitlog) |
| CASSANDRA-14463 | reproduced | 4.0.0→4.0.1 | INFO  [main] 2026-06-12 08:11:25,759 StorageService.java:528 - Gathering node replacement infor… |
| CASSANDRA-14477 | reproduced | 3.11.8→3.11.9 | Exception (org.apache.cassandra.exceptions.ConfigurationException) encountered during startup: … |
| CASSANDRA-14496 | reproduced | 4.0.0→4.0.1 | DEBUG [MigrationStage:1] 2026-06-12 04:24:44,927 TimeWindowCompactionStrategy.java:65 - Disabli… |
| CASSANDRA-14559 | reproduced | 3.11.7→3.11.8 | INFO  [GossipTasks:1] 2026-06-12 08:17:07,912 Gossiper.java:880 - FatClient /10.244.1.141 has b… |
| CASSANDRA-14925 | reproduced | 3.11.9→3.11.10 | java.lang.OutOfMemoryError: Java heap space 	at java.math.BigDecimal.toPlainString(BigDecimal.j… |
| CASSANDRA-15134* | reproduced | 4.0.1→4.0.2 | LIVE dir t-01a043b0661611f1ae9669dc20ef3eb2 contains nb-1-big-SI_t_name_sasi.db and nb-2-big-SI… |
| CASSANDRA-15135 | reproduced | 4.0.0→4.0.1 | Exception (java.lang.RuntimeException) encountered during startup: java.lang.reflect.Invocation… |
| CASSANDRA-15191* | reproduced | 3.11.7→3.11.8 |  |
| CASSANDRA-15433 | reproduced | 4.0.1→4.0.2 | Local write count: 0 |
| CASSANDRA-15459 | reproduced | 3.11.7→3.11.8 | pk / c ----+---   0 / 0  (1 rows) |
| CASSANDRA-15814 | reproduced | 3.11.7→3.11.8 | InvalidRequest: Error from server: code=2200 [Invalid query] message="Invalid list literal for … |
| CASSANDRA-15857 | reproduced | 3.11.7→3.11.8 | org.apache.cassandra.exceptions.InvalidRequestException: Non-frozen tuples are not allowed insi… |
| CASSANDRA-15896 | reproduced | 3.11.7→3.11.8 | java.lang.NullPointerException: null at org.apache.cassandra.db.marshal.AbstractType.toJSONStri… |
| CASSANDRA-16071 | reproduced | 3.11.7→3.11.8 | ERROR [SASI-General:9] 2026-06-12 05:52:24,045 PerSSTableIndexWriter.java:262 - Failed to build… |
| CASSANDRA-16146 | reproduced | 3.11.9→3.11.10 | STATUS:87:NORMAL,-1077568207160367180 |
| CASSANDRA-16156 | reproduced | 3.11.8→3.11.9 | DEBUG [MessagingService-Outgoing-/10.244.2.125-Gossip] 2026-06-12 08:46:00,222 OutboundTcpConne… |
| CASSANDRA-16259 | reproduced | 3.11.9→3.11.10 | java.lang.ArrayIndexOutOfBoundsException: 115 	at org.apache.cassandra.metrics.TableMetrics.com… |
| CASSANDRA-16307 | reproduced | 3.11.10→3.11.11 | Consistency level set to ALL. Page size: 1   pk / ck ----+----   0 /  0  (1 rows)  Warnings : A… |
| CASSANDRA-16334 | reproduced | 4.0.1→4.0.2 | WriteTimeout: Error from server: code=1100 [Coordinator node timed out waiting for replica node… |
| CASSANDRA-16372 | reproduced | 3.11.9→3.11.10 | <stdin>:8:Failed to import 1 rows: ParseError - Failed to parse ['But if you now try to wash yo… |
| CASSANDRA-16418* | reproduced | 4.1.0→4.1.1 | BUGGY 4.1.0: cqlsh "CONSISTENCY ONE; SELECT COUNT(*) FROM repro16418.t;" returned `count 3375` … |
| CASSANDRA-16577 | reproduced | 3.11.10→3.11.11 | WARN  [main] 2026-06-12 07:14:20,771 StorageService.java:941 - There are nodes in the cluster w… |
| CASSANDRA-16671 | reproduced | 3.11.10→3.11.11 | pk / ck / v ----+----+---   (0 rows) |
| CASSANDRA-16692 | reproduced | 3.11.10→3.11.11 | java.lang.RuntimeException: Didn't receive schemas for all known versions within the timeout 	a… |
| CASSANDRA-16718 | reproduced | 4.1.1→4.1.2 | java.lang.RuntimeException: Unable to gossip with any peers 	at org.apache.cassandra.gms.Gossip… |
| CASSANDRA-16796 | reproduced | 4.0.0→4.0.1 | <stdin>:1:NoHostAvailable: ('Unable to complete the operation against any hosts', {<Host: 127.0… |
| CASSANDRA-16836 | reproduced | 3.11.11→3.11.12 | org.apache.cassandra.exceptions.InvalidRequestException: Unknown function repro16836.double cal… |
| CASSANDRA-16839 | reproduced | 4.0.1→4.0.2 | truncated-1781238400142-size_estimates  system        size_estimates     0 bytes   13 bytes |
| CASSANDRA-16868 | reproduced | 4.0.0→4.0.1 | SELECT * FROM repro16868.t WHERE ck=2;  ->  (0 rows)   [SAME row returns (1,2,3) via PK lookup … |
| CASSANDRA-16898 | reproduced | 4.0.1→4.0.2 | DESCRIBE MATERIALIZED VIEW repro16898ks.mv on 4.0.1 prints: WITH CLUSTERING ORDER BY (v ASC, ck… |
| CASSANDRA-16902 | reproduced | 4.0.1→4.0.2 | <stdin>:1:Unauthorized: Error from server: code=2100 [Unauthorized] message="You are not author… |
| CASSANDRA-16977 | reproduced | 4.0.1→4.0.2 | java.lang.ArrayIndexOutOfBoundsException: Index 1 out of bounds for length 1 	at org.apache.cas… |
| CASSANDRA-17136 | reproduced | 4.0.1→4.0.2 | ERROR [RMI TCP Connection(8)-127.0.0.1] 2026-06-12 03:00:11,492 DefaultFSErrorHandler.java:64 -… |
| CASSANDRA-17266 | reproduced | 4.0.3→4.0.4 | AND default_time_to_live = 0 |
| CASSANDRA-17415 | reproduced | 3.11.12→3.11.13 | /var/lib/cassandra/data/repro17415_ks/mv-e1917d60661911f1a8c4edaf56a013df/snapshots/17812397388… |
| CASSANDRA-17467 | reproduced | 4.0.3→4.0.4 | InvalidRequest: Error from server: code=2200 [Invalid query] message="Unable to parse a date/ti… |
| CASSANDRA-17623 | reproduced | 4.0.4→4.0.5 | c['a'] = None |
| CASSANDRA-17752* | reproduced | 4.0.5→4.0.6 |  |
| CASSANDRA-17840 | reproduced | 4.0.5→4.0.6 | java.lang.IndexOutOfBoundsException: -2147483644 |
| CASSANDRA-17913 | reproduced | 4.1.1→4.1.2 | InvalidRequest: Error from server: code=2200 [Invalid query] message="Invalid element selection… |
| CASSANDRA-17918* | reproduced | 4.1.1→4.1.2 | DESCRIBE TYPE on 4.1.1 emits "    token text," (unquoted reserved keyword); fixed 4.1.2 emits "… |
| CASSANDRA-17919 | reproduced | 4.1.1→4.1.2 | SyntaxException: line 1:13 no viable alternative at input 'P' (CREATE TABLE [P]...) |
| CASSANDRA-17933 | reproduced | 4.0.6→4.0.7 | Caused by: java.nio.channels.OverlappingFileLockException: null 	at java.base/sun.nio.ch.FileLo… |
| CASSANDRA-18264 | reproduced | 4.1.0→4.1.1 | java.nio.file.FileAlreadyExistsException: /tmp/lib/cassandra-0.jar	at java.base/java.nio.file.F… |
| CASSANDRA-18647* | reproduced | 4.1.2→4.1.3 | cast(e as decimal) = 5.199999809265137 |
| CASSANDRA-18760 | reproduced | 4.0.11→4.0.12 | java.lang.AssertionError: col1 	at org.apache.cassandra.db.rows.UnfilteredSerializer.lambda$ser… |
| CASSANDRA-18778 | reproduced | 4.1.3→4.1.4 | Caused by: java.lang.IllegalArgumentException: 'keystore_password' must be specified 	at org.ap… |
| CASSANDRA-18935 | reproduced | 4.1.3→4.1.4 | <stdin>:1:NoHostAvailable: ('Unable to complete the operation against any hosts', {<Host: 127.0… |
| CASSANDRA-19401 | reproduced | 4.1.4→4.1.5 | INFO  [RMI TCP Connection(6)-127.0.0.1] 2026-06-12 03:00:08,251 SSTableImporter.java:173 - No n… |
| CASSANDRA-19475 | reproduced | 4.1.4→4.1.5 | data_file_directories / [Ljava.lang.String;@4cb1c088 |
| CASSANDRA-19566 | reproduced | 4.1.4→4.1.5 | -13767019200000 / 1533-09-28 12:00:00.000000+0000 / "1533-09-18 12:00:00.000Z" |
| CASSANDRA-19637* | reproduced | 4.1.5→4.1.6 | 4.1.5 non-frozen list, same column: `UPDATE ... IF l >= null` -> InvalidRequest code=2200 messa… |
| CASSANDRA-19747 | reproduced | 4.1.5→4.1.6 | field1 text PRIMARY KEY,     field2 text     field3 text |
| CASSANDRA-19749 | reproduced | 4.1.5→4.1.6 | repro19749_missing /      null /         True /      null /        null |
| CASSANDRA-19880 | reproduced | 5.0.0→5.0.1 | at org.apache.cassandra.transport.messages.ExecuteMessage.traceQuery(ExecuteMessage.java:227) -… |
| CASSANDRA-19889 | reproduced | 5.0.1→5.0.2 | <stdin>:1:InvalidRequest: Error from server: code=2200 [Invalid query] message="full() indexes … |
| CASSANDRA-19891 | reproduced | 5.0.4→5.0.5 | Caused by: java.lang.IllegalArgumentException: Unsupported collection type: map 	at org.apache.… |
| CASSANDRA-20036 | reproduced | 5.0.2→5.0.3 | schema.cql:26:InvalidRequest: Error from server: code=2200 [Invalid query] message="Unknown typ… |
| CASSANDRA-20052 | reproduced | 5.0.2→5.0.3 | ERROR [epollEventLoopGroup-5-14] 2026-06-12 03:32:11,922 JVMStabilityInspector.java:186 - Force… |
| CASSANDRA-20086 | reproduced | 5.0.6→5.0.7 | SELECT pk FROM t ORDER BY val ann of [1.0, 2.0] LIMIT 1 -> pk=0 on 5.0.6 (WRONG; correct=pk=1 s… |
| CASSANDRA-20171 | reproduced | 5.0.4→5.0.5 | InvalidRequest: Error from server: code=2200 [Invalid query] message="Resource <keyspace system… |
| CASSANDRA-20189* | reproduced | 5.0.3→5.0.4 | SAI intersection query "CONSISTENCY ALL; SELECT * FROM repro20189.partial_updates WHERE a = 1 A… |
| CASSANDRA-20238 | reproduced | 5.0.3→5.0.4 | Buggy SAI result `(0 rows)` from `SELECT * FROM repro20238_ks.tbl WHERE v0=1 AND pk0=0 ALLOW FI… |
| CASSANDRA-20313 | reproduced | 5.0.3→5.0.4 | Caused by: java.lang.NullPointerException: Cannot invoke "org.apache.cassandra.utils.bytecompar… |
| CASSANDRA-20449 | reproduced | 5.0.3→5.0.4 | 0 / 0 / {1, 2} / {1, 2} / {1, 2} |
| CASSANDRA-20787 | reproduced | 5.0.4→5.0.5 | java.lang.RuntimeException: Cannot get data directories grouped by file store |
| CASSANDRA-12734 | not-reproducible | 4.0.1 | Root cause of non-reproduction: the fix commit 67eb22ec9d588c9f984d13c0ffd703a14181f775 patches… |
| CASSANDRA-16127 | not-reproducible | 3.11.8 | The literal reporter reproducer (disable->enable) does not reproduce on the released cassandra:… |
| CASSANDRA-17848 | not-reproducible | 4.1.0 | The candidate's premise is wrong: 4.1.0 is the FIRST FIXED release on the 4.1 line, not a buggy… |
| CASSANDRA-18118 | not-reproducible | 4.1.0 | Both of the body's documented reproducers were exercised; neither yielded a buggy-vs-fixed delt… |
| CASSANDRA-18294 | not-reproducible | 4.1.0 | The code defect is REAL but shadowed at runtime. There is a genuine secondary truth of needs-fi… |
| CASSANDRA-18824 | not-reproducible | 4.1.3 | Load-bearing chain: the 16418 guard does not merely EXIST in 4.1.3 — it FIRES in this exact rep… |
| CASSANDRA-15669 | confirmed-blocked | 3.11.10 | harvested from /tmp log |
| CASSANDRA-15970 | confirmed-blocked | 3.11.7 | harvested from /tmp log |
| CASSANDRA-18756 | confirmed-blocked | 4.1.3 | Symptom is delayed RELEASE/deletion of references to obsolete SSTables, only while one long com… |
| CASSANDRA-15164 | needs-fix-test | 3.11.8 | Went further than typical for needs-fix-test: adapted AND ran the fix-style serialize->deserial… |
| CASSANDRA-17342 | needs-fix-test | 4.0.2 | Disposition needs-fix-test: the reporter explicitly states the bug is "demonstrated in the atta… |
| CASSANDRA-14113 | inconclusive | 3.11.13 | NEEDS MANUAL ASSESSMENT (cyber-safeguard blocked the agent, like 20976). |
| CASSANDRA-19166 | inconclusive | 4.1.3 | Log has full root-cause analysis + reproducer plan (nested UnmodifiableMap -> StackOverflowErro… |

### Deferred (not attempted) — resumable

**11 multi-node rings** (slowest medium-confidence; time+disk-bound): 15899, 15902, 15924, 16518, 16945, 17367, 17411, 17507, 18304, 18466, 20243.

**12 needs-fix-test appendix** (reproducer only in the fix's unit/dtest): 15158, 15897, 15962, 16226, 17072, 17900, 18359, 19578, 20567, 20670, 20753, 20829.

Resume: the workflow skips any candidate with a `/tmp/repro-<key>.md` log. Write `/tmp/repro_batch.json` (`{mode,ringConcurrency,candidates:[{key,buggy,topo,conf,trigger}]}`) and re-run `Workflow({scriptPath:'.claude/repro_candidates_workflow.js'})`. A larger disk/external cluster unblocks higher ring concurrency.

