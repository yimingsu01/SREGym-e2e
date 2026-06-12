# Reproduction progress — bugs.txt (Apache Cassandra DB-behavior bugs)

## Scope (current)
> **bugs.txt re-curation (2026-06-11).** After Phase 3, `bugs.txt` was pruned to keep only the **11
> reproduced** bugs and extended with **100 freshly-collected candidates** (+ a 12-item needs-fix-test
> appendix) drawn from a new Apache Jira sweep (1800 recent fixed bugs → 671 deployable → 433 after a
> test-infra pre-filter → classified by a 25-agent workflow → 174 stageable/observable/in-body-or-derivable,
> of which 100 were selected, 25/category, high-confidence first; 58 high + 42 medium). Candidates are
> UNVERIFIED (text-triaged). The 40 non-reproduced bugs removed from the old list remain documented below
> and in `repro-findings.md`. Classifier verdicts: `.claude/repro-evidence/classify_verdicts.json`.

The notes below describe the original triage that produced the reproduced set. `bugs.txt` was re-triaged
from the original 100 cached Jira issues and rewritten to contain **only DB-behavior bugs** (CI/test-logic
and internal-tooling dropped). **51 bugs** were carried into Phase 3, grouped: cql-semantics 13,
storage-engine 9, distributed-multinode 23, other-db-behavior 6.

Reproduction uses the **stock-image fast path**: for a bug fixed in a released `X.Y.Z`, the buggy
version is `patch − 1` and the official `cassandra:<buggy>` image already contains the bug, so a
single stock pod (or, for distributed bugs, a multi-node ring) reproduces it (no source build). Where a
fixed image exists, the same workload is run on the fixed version as an A/B control. Helpers:
`repro_helper.sh` (single-node), and the multi-node StatefulSet recipe in `.claude/repro_workflow.js`.

- Deployable (released image): **19 / 51**
- Trunk-only (no released image, fixed only in 6.0-alpha/6.0/7.x): **32 / 51**

## Outcome summary
| Disposition | Count | Bugs |
| --- | --- | --- |
| ✅ reproduced (in kind) | **11** | 20050, 21348, 21065, 20972, 21057, 21092, **21219, 21290, 21332, 20877, 21132** |
| not-reproducible (path shadowed) | 2 | 20915, 20982 |
| not-observable (internal refinement/hardening) | 2 | 20917, 21389 |
| confirmed-blocked (attempted; specific un-stageable mechanism) | 4 | 21245, 20871, 21428, 20976 |
| blocked-no-image (trunk-only) | 32 | all `deployable=0` |

Totals: 11 + 2 + 2 + 4 + 32 = 51.

> **Phase 3 update (2026-06-11).** A 4-node kind cluster enabled real multi-node rings. Of the 9
> previously blocked-hard/blocked-risk *deployable* bugs, **5 reproduced** (21219, 21290, 21332, 20877,
> 21132) — three of them overturning the prior verdict with evidence — and **4 are confirmed-blocked**
> (21245, 20871, 21428, 20976), each with a specific mechanism that cannot be staged with `kubectl exec`
> on stock pods. Per-bug evidence logs: `.claude/repro-evidence/repro-CASSANDRA-<n>.md`. Full write-ups
> in `repro-findings.md` Parts 3-4. This session was **record-only**: no SREGym tooling or Cassandra
> source was modified; every agent ran manual/hand-crafted mode and reported `tooling_findings: none`.

## Reproduced bugs (11; all with controls — see `repro-findings.md` Part 3)
| Bug | Buggy → Fixed | Category | One-line trigger |
| --- | --- | --- | --- |
| CASSANDRA-20050 | 4.0.14 → 4.0.15 | cql-semantics | `frozen<UDT>` clustering key + `CLUSTERING ORDER BY DESC` rejects a valid INSERT |
| CASSANDRA-21348 | 5.0.8 (+config) | cql-semantics | `check_data_resurrection` on → `SELECT system_views.settings` `ClassCastException` |
| CASSANDRA-21065 | 5.0.6 → 5.0.7 | storage-engine | `nodetool garbagecollect` (UCS + `only_purge_repaired_tombstones`, ≥2 unrepaired sstables) → CME |
| CASSANDRA-20972 | 5.0.5 → 5.0.6 | storage-engine | range tombstone + higher-ts row + `SELECT DISTINCT … token(id)>MIN` → `IllegalStateException` |
| CASSANDRA-21057 | 4.1.10 → 4.1.11 | other-db-behavior | trip disk-usage guardrail FULL, disable threshold → gossip `DISK_USAGE` stuck `FULL` |
| CASSANDRA-21092 | 5.0.6 → 5.0.7 | distributed-multinode | `sstableloader` 3.11 sstables w/ zero-copy → `AssertionError: Filter should not be serialized in old format` |
| CASSANDRA-21219 | 5.0.6 → 5.0.7 | cql-semantics (security) | non-superuser `ADD IDENTITY … TO ROLE cassandra` binds its cert id to a superuser role (CVE-2026-27314); **no mTLS PKI needed** |
| CASSANDRA-21290 | 4.1.11 (no fixed image) | other-db-behavior | 0-byte `cassandra-heartbeat` file (the crash artifact) → `Failed to deserialize heartbeat file` → startup aborts (exit 3) |
| CASSANDRA-21332 | 5.0.8 (no fixed image) | cql-semantics (SAI/RFP) | 3-node divergent data + SAI static col + `read_repair=NONE` → `SELECT … WHERE s1=42` resurrects range-tombstoned rows (3 rows vs 1) |
| CASSANDRA-20877 | 4.0.19 → 4.0.20 | distributed-multinode | incremental repair finalizes, then range movement (scale 2→3) → FINALIZED `system.repairs` row never cleaned on the coordinator |
| CASSANDRA-21132 | 5.0.6 (fix is opt-in) | distributed-multinode | 324 SAI indexes + cold cluster restart → legacy `INDEX_STATUS` gossip overflows `Short.MAX_VALUE` → `AssertionError` in GossipStage, join deadlock |

## Method notes
- A bug's **buggy version is the released fix patch − 1** (e.g. fix 5.0.6 ⇒ buggy 5.0.5). Running on
  the fix version is itself the control (proven on 20972: 5.0.5 fails, 5.0.6 clean).
- Single-node pod suffices for cql-semantics and most storage bugs (`nodetool flush/garbagecollect`).
  `disableautocompaction` is needed to retain ≥2 sstables for compaction-iteration bugs (21065).
- Config-gated bugs (21348, 21290) are reproduced by appending an active block to `cassandra.yaml` in
  the pod `command` before `docker-entrypoint.sh cassandra -f`.
- Cross-version bugs (21092) are reproduced by generating sstables on a 3.11.19 pod and `sstableloader`-ing
  them into a 5.0.x pod.
- **Multi-node (Phase 3):** rings are deployed as a headless `Service` + `StatefulSet`
  (`podManagementPolicy: OrderedReady`, seed = `cass-0`, `volumeClaimTemplates` when schema must survive a
  restart). Verify the ring with `nodetool status` (N × `UN`). For per-replica divergence without an
  in-JVM dtest, use **gossip isolation**: `nodetool disablegossip` on the peers, confirm `DN` from the
  writer's view, write at `CONSISTENCY ONE`, `nodetool flush`, then re-enable gossip; verify the physical
  divergence with `sstabledump` on each node's local `Data.db` (a `CL=ONE` read is routed to an arbitrary
  replica and is **not** a reliable per-node probe — 21332).

## What is NOT reproduced, and why (see `repro-findings.md` Part 4 for the full table)
- **2 internal-path bugs (20915, 20982):** the buggy code is shadowed by earlier client validation /
  a disabled feature, so cqlsh cannot reach it.
- **2 internal refinements (20917, 21389):** no client-visible behavior change to assert.
- **4 confirmed-blocked (attempted in Phase 3):**
  - **21245** — premise refuted: every disk-space check on a single small pod used the **compressed**
    size, not uncompressed; the uncompressed `… is needed` figure only appears under sustained
    concurrent-write load over a large multi-GiB STCS bucket (reporter: 1.1 TiB, 31 pending compactions),
    un-stageable on one small pod.
  - **20871** — the length-0 counter context that crashes `CounterContext.headerLength` is produced only
    by an in-JVM dtest `executeInternal` (uncoordinated node-local counter write); cqlsh/nodetool route
    through the counter leader and always yield a non-empty context.
  - **21428** — `ECHO_REQ` and gossip multiplex on the same internode TCP/7000 connection, so a
    connection-level partition drops both; the failure detector convicts the peer within the echo-timeout
    window and `silentlyMarkDead` clears the stale `inflightEcho` entry (the bug's escape hatch). The
    required state (ECHO failed **while** FD healthy) needs an in-JVM `IMessageFilters` verb-drop.
  - **20976** — the Jira body is **only** a mailing-list URL with no concrete reproducer (verified by
    reading `/tmp/jira_issues/CASSANDRA-20976.json`; 64-byte description).
- **32 trunk-only:** fixed only in 6.0-alpha/6.0/7.x → no released image (re-confirmed this session: no
  `cassandra:6.0`/`5.0.9`/`4.0.21`/`4.1.12`/`7.0` tags on Docker Hub; ceilings 3.11→19, 4.0→20, 4.1→11,
  5.0→8 hold) → would need a custom trunk build (out of scope; this session does not build with docker).

## Cluster state
After Phase 3, all reproduction agents tore down the namespaces they created (`repro-*`, `ctrl-20877`);
`repro-20976` (left by the one agent that errored out — see Phase 3) was removed manually. Remaining
namespaces are the prior single-node version pods (`cass-3-11-19`, `cass-4-0-18/19/20`, `cass-4-1-10/11`,
`cass-5-0-5/6`, `cass-21348`=5.0.8+config, `cass-21245`=5.0.8+lever, `repro-smoke`=5.0.8) and the original
20050 3-node K8ssandra cluster in `k8ssandra-operator`.

---

## Follow-up (Phase 3): the 9 previously-blocked deployable bugs — COMPLETE
Directive: **skip** the not-reproducible (20915, 20982), not-observable (20917, 21389), and trunk-only
(32) bugs; **attempt** the 8 blocked-hard + 1 blocked-risk on a 4-node kind cluster
(`kind-control-plane` + `kind-worker{1,2,3}`). Reproductions were fanned out one agent per bug via
`.claude/repro_workflow.js` (a gated preflight, then 9 parallel agents, then a trunk-only re-check).

Image ceilings still hold (Docker Hub has **no** 5.0.9 / 4.0.21 / 4.1.12 / 6.0 / 7.x). So fixed-image
controls exist for 21219→5.0.7, 20871→4.0.20, 20877→4.0.20, 21132→5.0.7; 21245/21290/21332/21428 have no
fixed image → within-version evidence.

| Bug | Buggy | Topology | Outcome | Notes |
| --- | --- | --- | --- | --- |
| 21219 | 5.0.6 | single | ✅ **reproduced** | Privilege escalation needs **no mTLS PKI** (prior verdict over-scoped). `bob` (CREATE-only) bound its identity to the superuser role under `PasswordAuthenticator`; control 5.0.7 rejects with `Unauthorized … Only superusers can bind identities`. |
| 21290 | 4.1.11 | single | ✅ **reproduced** | Pre-staged the 0-byte heartbeat artifact → `CassandraDaemon.java:900 - Failed to deserialize heartbeat file` → exit 3 / CrashLoop. Caveat: the crash *race* was not raced; only the read-empty-file path the fix addresses. `check_data_resurrection` is OFF by default. |
| 21332 | 5.0.8 | 3-node RF=3 | ✅ **reproduced** | Per-replica divergence staged via gossip isolation (verified by `sstabledump`); SAI query `WHERE s1=42` at `CL=ALL` returns 3 rows (2 tombstoned rows resurrected) vs 1 correct. Disproves "in-JVM-dtest-only". |
| 20877 | 4.0.19 | 3-node (2→3) | ✅ **reproduced** | Differential on the S2 coordinator: 4.0.19 logs `LocalSessions.java:456 - Skipping delete of FINALIZED LocalSession … not been superseded` and keeps the row; 4.0.20 logs `:487 - Auto deleting repair session` and removes it. |
| 21132 | 5.0.6 | 2-node | ✅ **reproduced** | 324 SAI indexes + cold bring-down/up → `AssertionError at TypeSizes.sizeof(TypeSizes.java:44)` in GossipStage serializing `GossipDigestAck`; cass-1 stuck `DN`. Control NOT run — the fix is **opt-in** (`force_optimized_index_status_format`), so a naive 5.0.7 A/B would still reproduce. |
| 21245 | 5.0.8 | single | ⛔ **confirmed-blocked** | Premise refuted: every space check used the **compressed** size (213,951 B), not uncompressed; verified across `compact`/`garbagecollect`/`upgradesstables`/`scrub` (all rc=0) + bytecode read of `getExpectedCompactedFileSize`. The uncompressed `… is needed` manifestation requires sustained concurrent write over a large multi-GiB STCS bucket. |
| 20871 | 4.0.19 | single | ⛔ **confirmed-blocked** | Empty counter context only producible by in-JVM dtest `executeInternal` (uncoordinated local write); cqlsh/nodetool always produce non-empty contexts. Code path is reachable (not shadowed), but the precondition is not stageable externally. |
| 21428 | 4.0.20 | (not deployed) | ⛔ **confirmed-blocked** | Structural pre-deploy blocker: ECHO_REQ + gossip share one TCP/7000 connection; connection-level partitioning convicts the peer (FD) within the echo timeout → `silentlyMarkDead` clears the stale `inflightEcho` (the escape hatch). Needs in-JVM `IMessageFilters` verb-drop. |
| 20976 | 5.0.5 | n/a | ⛔ **confirmed-blocked** | No concrete reproducer — Jira description is a single mailing-list URL (verified). The assigned agent also hit a cyber-safeguard API false-positive; disposition stands on the merits (read the JSON directly). |

### Notes on the reproductions that overturned prior verdicts
- **21219** was prior-labeled "needs full mTLS PKI". Source (`AddIdentityStatement.authorize`) and
  experiment both show the authorization gate has no authenticator dependency — `ADD IDENTITY` executes
  and writes `system_auth.identity_to_role` under plain `PasswordAuthenticator`. mTLS is only the
  downstream *use* of the bound identity, not the bug, so the PKI would be gold-plating.
- **21332** was prior-labeled "likely in-JVM-dtest-only". The dtest's per-replica `executeInternal`
  divergence was reproduced on a real ring via gossip isolation + `CL=ONE` writes + `sstabledump`
  verification.
- **21290** was prior-labeled "likely non-deterministic". The non-deterministic part is the crash race;
  the *manifestation the fix addresses* (failing to parse an empty heartbeat file at startup) is
  deterministic and was staged directly.
