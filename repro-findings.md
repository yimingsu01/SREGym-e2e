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

## Part 3 — Reproductions achieved in the kind cluster (6)

All reproductions deploy a stock single-node `cassandra:<buggy>` pod (heap capped at 1G so many run
concurrently), drive the reproducer via `kubectl exec … cqlsh`/`nodetool`/`sstableloader`, and — where
a fixed image exists — run the **identical** workload on the fixed version as an A/B control. The
reusable deploy/wait/cql/teardown helper is in the session files dir (`repro_helper.sh`).

| # | Bug | Buggy → Fixed | Category | Trigger | Control |
| - | --- | --- | --- | --- | --- |
| 1 | CASSANDRA-20050 | 4.0.14 → 4.0.15 | cql-semantics | `frozen<UDT>` clustering key + `CLUSTERING ORDER BY (loc DESC)` rejects a valid INSERT | ASC same schema inserts OK |
| 2 | CASSANDRA-21348 | 5.0.8 (config) | cql-semantics | `check_data_resurrection` startup_check enabled → `SELECT … system_views.settings` throws `ClassCastException` | stock 5.0.8 (no config) returns rows |
| 3 | CASSANDRA-21065 | 5.0.6 → 5.0.7 | storage-engine | `nodetool garbagecollect` on UCS + `only_purge_repaired_tombstones` with ≥2 unrepaired sstables → `ConcurrentModificationException` | fixed 5.0.8 runs clean |
| 4 | CASSANDRA-20972 | 5.0.5 → 5.0.6 | storage-engine | range tombstone + higher-ts row + `SELECT DISTINCT … token(id) > MIN` → server `IllegalStateException` | fixed 5.0.6 returns rows |
| 5 | CASSANDRA-21057 | 4.1.10 → 4.1.11 | other-db-behavior | trip disk-usage guardrail FULL, then disable threshold → gossip `DISK_USAGE` stays `FULL` | fixed 4.1.11 → `NOT_AVAILABLE` |
| 6 | CASSANDRA-21092 | 5.0.6 → 5.0.7 | distributed-multinode | `sstableloader` legacy 3.11 sstables with zero-copy → server `AssertionError: Filter should not be serialized in old format` | fixed 5.0.8 loads 500 rows clean |

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

## Part 4 — Bugs assessed but not reproduced (disposition)

| Bug | Buggy ver | Disposition | Reason |
| --- | --- | --- | --- |
| CASSANDRA-20915 | 4.0.18 | not-reproducible | `CREATE KEYSPACE` is rejected earlier by client-side validation with the **correct** "48 characters" message; the buggy `222` constant lives only in the internal `KeyspaceMetadata.validateKeyspaceName` path (unit tests). |
| CASSANDRA-20982 | 4.0.19 | not-reproducible | `ALTER … TYPE` is fully disabled in 4.0 ("Altering column types is no longer supported"); the buggy `isValueCompatibleWith` check is reachable only from unit tests. |
| CASSANDRA-20917 | 5.0.5 | not-observable | Internal error-type refinement (throw RTE instead of FSError in `TOCComponent`); no distinct client-visible behavior. |
| CASSANDRA-21389 | 4.0.20 | not-observable | Server-side snapshot-name hardening (validation); no client-visible misbehavior in normal use. |
| CASSANDRA-21219 | 5.0.6 | blocked-hard | CVE-2026-27314 privilege escalation needs full mTLS setup (`MutualTlsAuthenticator` + client cert truststore/keystore + roles) before `ADD IDENTITY` authz can be tested. |
| CASSANDRA-20871 | 4.0.19 | blocked-hard | Counter + repaired-data AIOOBE needs `repaired_data_tracking_*` on (yaml+restart), counter cells in **repaired** sstables (no second node to mark repaired), and an empty counter context. |
| CASSANDRA-21332 | 5.0.8 | blocked-hard | Static-SAI + range-tombstone resurrection is an in-JVM multi-node dtest requiring per-node divergent data, `read_repair=NONE`, and the replica-filtering-protection path. |
| CASSANDRA-21245 | 5.0.8 | blocked-risk | Compressed-table compaction uses **uncompressed** size in the disk-space check. Reproducing needs data-dir free space < the table's uncompressed size; the kind node fs is **shared** by all repro pods, so filling it risks crashing co-located pods. Reproducer is otherwise well-understood. |
| CASSANDRA-20877 | 4.0.19 | blocked-hard | FINALIZED incremental-repair cleanup after range movement; needs ≥2 nodes, incremental repair, bootstrap/decommission, and cleanup-interval timing. |
| CASSANDRA-21132 | 5.0.6 | blocked-hard | SAI index-status gossip startup deadlock; needs a homogeneous multi-node cluster with many SAI indexes to trip the gossip-encoding feature-gate race. |
| CASSANDRA-21428 | 4.0.20 | blocked-hard | Nodes stuck DOWN after `ECHO_REQ` timeout; needs multi-node + a transient partition with precise echo-timeout/recovery timing. |
| CASSANDRA-20976 | 5.0.5 | blocked-hard | BTI-sstable `AssertionError` on token-range query; description is only a mailing-list link with no concrete reproducer. |
| CASSANDRA-21290 | 4.1.11 | blocked-hard | Atomic heartbeat-file write; bug needs a crash in the file create→write window (hardening), not deterministically reproducible. |
| 32 trunk-only bugs | — | blocked-no-image | Fixed only in `6.0-alpha*/6.0/7.x`; no released `X.Y.Z` image. Would need a custom trunk source build (and a matching base image, likely absent). |

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
