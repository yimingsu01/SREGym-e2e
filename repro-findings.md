# Reproduction findings — bugs.txt (100 Apache Cassandra Jira issues)

Two kinds of findings are recorded here:
1. **Bugs in the SREGym reproduction tooling** that block the skill's automatic path. Per the
   session instruction these are **only documented, not fixed**.
2. **Findings about the 100 Cassandra bugs themselves** — how many are actually reproducible in a
   kind cluster, and the reproduction(s) that were achieved.

---

## Part 1 — Bugs found in the SREGym tooling (NOT fixed this session)

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

> Net: the skill's **automatic** path is unusable for these 100 Jira issues. Reproduction must use
> the skill's documented **hand-crafted** mode (`db_version` + `source_git_ref` + explicit
> `reproducer`), which bypasses the Jira parser and the extractor. That is the path used below.

---

## Part 2 — Reproducibility of the 100 Cassandra bugs

All 100 entries are Apache Jira `Bug`-type issues. They were fetched via the public Jira REST API
and triaged (raw JSON cached in `/tmp/jira_issues/`).

### Two hard gates for "reproducible in kind via the skill"
1. **Deployable image:** the buggy build must sit on a real `k8ssandra/cass-management-api:<ver>-ubi8`
   base image. That requires the fix to have shipped in a **released** `X.Y.Z` version (so the
   version just below it is a released, image-backed buggy version).
   - **36 / 100** have a released `X.Y.Z` fix version.
   - **64 / 100** were fixed only in `6.0-alpha*` / `6.0` / `7.x` / trunk → **no deployable base
     image** → cannot be deployed by the skill's K8ssandra path.
2. **cqlsh-triggerable:** the bug must be triggerable by a short CQL sequence against a single
   freshly-deployed node (no multi-node repair/streaming/gossip, no JVM unit-test harness, no
   special flush/compaction timing).

### Category breakdown (by Jira components + description)
| Category | Count | cqlsh-reproducible on a single node? |
| --- | --- | --- |
| ci-test-infra (junit XML, CI config, flaky/dtest) | 27 | No |
| other-internal (internal logic seen in tests) | 32 | No |
| distributed-multinode (repair / streaming / gossip / coordination) | 12 | No |
| internal-tooling (nodetool / metrics / virtual tables / messaging) | 8 | Rarely |
| storage-engine (compaction / sstable / memtable / commitlog) | 7 | Rarely (needs flush+compaction sequencing) |
| cql-semantics (CQL/Semantics, CQL/Syntax, Feature/*) | 14 | Some — the only real candidates |
| **Total** | **100** | |

Intersection of **cql-semantics ∧ released fix ∧ in-description CQL reproducer = 1 issue:
CASSANDRA-20050.** A handful of other cql-semantics issues have released fixes but no ready CQL
reproducer in the description (assessed individually — see the candidate notes appended below).

### Conclusion on scope
A genuine "reproduce all 100 in kind" is **not achievable**: the automatic pipeline is blocked by
BUG-1/2/3, and even with unlimited hand-crafting effort the **large majority of these issues are
not reproducible through a deployed cqlsh cluster** (they are CI/test, internal-logic,
multi-node-repair, or trunk-only-without-an-image bugs). The realistic reproducible set is a
**small handful**, led by CASSANDRA-20050.

---

## Part 3 — Reproductions achieved in the kind cluster

### CASSANDRA-20050 — UDT/vector clustering key in DESC order rejects valid INSERT
- **Problem:** `sregym/conductor/problems/auto_cassandra_20050.py` (hand-crafted; buggy `4.0.14`,
  `source_git_ref=cassandra-4.0.14`; fixed in 4.0.15 / 4.1.8 / 5.0.3).
- **Reproducer (cqlsh):**
  ```sql
  CREATE KEYSPACE udt_ks WITH REPLICATION = {'class':'SimpleStrategy','replication_factor':1};
  USE udt_ks;
  CREATE TYPE point (x int, y int);
  CREATE TABLE events (id int, loc frozen<point>, val text, PRIMARY KEY (id, loc))
      WITH CLUSTERING ORDER BY (loc DESC);
  INSERT INTO events (id, loc, val) VALUES (1, {x: 10, y: 20}, 'data');
  ```
- **Expected (buggy 4.0.14):** the INSERT is rejected with
  `InvalidRequest … Invalid user type literal for loc of type frozen<point>`.
  With `CLUSTERING ORDER BY (loc ASC)` the identical INSERT succeeds.
- **Status:** ✅ **REPRODUCED in the kind cluster** (2026-06-11).
  - Built buggy `4.0.14` from source (`cassandra-4.0.14` tag, `ant jar` in `eclipse-temurin:11`),
    loaded into kind. Deployed a 3-node K8ssandra cluster; the deployed image
    `docker.io/k8ssandra/cass-management-api:4.0.14-ubi` **is** the buggy 4.0.14 build, so the
    running cluster exhibits the bug directly (no image swap needed — fix landed only in 4.0.15+).
  - Ran the reproducer via `cqlsh` inside pod `sregym-cassandra-dc1-default-sts-0`
    (`-c cassandra`, authenticated with the `sregym-cassandra-superuser` secret) against
    `sregym-cassandra-dc1-service`:

    ```text
    # DESC clustering order (buggy path):
    <stdin>:12:InvalidRequest: Error from server: code=2200 [Invalid query]
      message="Invalid user type literal for loc of type frozen<point>"
    command terminated with exit code 2          # <-- non-zero => bug present (NotReady oracle)

    # ASC control (identical schema, only ORDER BY direction changed):
     id | loc            | val
    ----+----------------+------
      1 | {x: 10, y: 20} | data
    (1 rows)                                       # exit code 0 => control passes
    ```
  - **Conclusion:** the failure is isolated to `CLUSTERING ORDER BY (loc DESC)` on a `frozen<UDT>`
    clustering key, exactly matching the documented root cause (DESC wraps the column type in
    `ReversedType`; the UDT-literal validation path in `UserTypes.java` casts the wrapped type
    directly instead of calling `unwrap()`). The positive ASC control rules out a generic UDT
    problem. **Bug genuinely reproduced in kind.**

### Environment/tooling issues surfaced during the 20050 deploy (NOT fixed — noted only)

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

## Part 4 — Other CQL candidates assessed (and why they were not reproduced)

Each `cql-semantics` issue with a released fix was individually assessed for single-node
cqlsh-reproducibility. Result: **none** is a clean fit; CASSANDRA-20050 remains the only one.

| Bug | Buggy ver | cqlsh-only? | Why not reproduced |
| --- | --- | --- | --- |
| CASSANDRA-20915 | 4.0.18 | YES | Error-message-only diff: buggy says max keyspace-name length `222`, fixed says `48`. Both **reject** the DDL, so an exit-code / wrong-result oracle can't tell them apart — only the error *text* differs. Weak/unreliable oracle. |
| CASSANDRA-21348 | 5.0.8 | NO | `SELECT * FROM system_views.settings` throws only after a **non-default `cassandra.yaml`** (`startup_checks`) is set before start — not pure cqlsh on a fresh node. |
| CASSANDRA-21219 | 5.0.6 | NO | Privilege-escalation via `ADD IDENTITY` requires `MutualTlsAuthenticator` + client-cert identity setup, not default single-node cqlsh. |
| CASSANDRA-21057 | 4.1.10 | NO | Disk-usage guardrail: needs the disk filled past a threshold + `nodetool setguardrailsconfig`; gossip-state assertion, not cqlsh. |
| CASSANDRA-20871 | 4.0.19 | NO | `ArrayIndexOutOfBoundsException` in repaired-data tracking for counters — needs repair + SSTable state. |
| CASSANDRA-21332 | 5.0.8 | NO | SAI + range-tombstone resurrection requires 3 replicas with deliberately divergent writes (replica-filtering-protection path). |
| CASSANDRA-21245 | 5.0.8 | NO | Compressed-table sizing during compaction — needs large data volume + compaction + disk-space conditions. |
| CASSANDRA-21055 | trunk-only | MAYBE | Clean CQL shape (`UPDATE … SET col[0]=42 WHERE pk=0` on a non-existent pk) but fixed **only in 6.0-alpha1/6.0** → no deployable released image; behavior is thread/assertion-dependent. |
| CASSANDRA-21046 | trunk-only | MAYBE | Clean CQL DDL (silently-accepted `security_label`/bogus options) but fixed **only in 6.0-alpha1** → no deployable released image. |
| CASSANDRA-20877 | 4.0.19 | NO | FINALIZED repair-session cleanup after range movement — needs incremental repair + topology change + time-based cleanup. |

> Two structurally-clean CQL bugs (21055, 21046) are blocked purely by the "no released base image"
> gate; if the K8ssandra path supported building a deployable image from a trunk ref they would be
> the next candidates to hand-craft.
