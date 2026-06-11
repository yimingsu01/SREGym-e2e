# Plan — Re-scope bugs.txt to DB-behavior bugs, then reproduce

## User request
1. Update `bugs.txt`: keep only **DB-behavior** bugs. Drop **CI/test-logic** and **internal-tooling**.
   Explicitly include **storage-engine** (try them) and **all cql-semantics** (reproduce all).
   Multi-node kind cluster is available → distributed bugs are in scope.
2. After updating bugs.txt, **start reproducing** the bugs in the kind cluster.

## Context already in hand
- 100 cached Jira JSONs in `/tmp/jira_issues/`.
- Prior heuristic triage in `/tmp/repro_triage.db` (table `t`): cql-semantics 14, storage-engine 7,
  distributed-multinode 12, internal-tooling 8, ci-test-infra 27, other-internal 32.
- Tooling bugs (Jira parser/extractor/timeout/openebs) already FIXED last session.
- 4-node kind cluster UP (1 control-plane + 3 workers) → multi-node capable.
- 20050 already reproduced (cql-semantics, buggy 4.0.14).

## Phase 1 — Re-triage (parallel sub-agents) → new bugs.txt
Re-classify all 100 from the cached JSON with a strict rubric (prior category = hint only).
Output per bug: category, is_db_behavior, has_reproducer, repro_method, confidence, reason.
- 5 batches × 20 bugs → `/tmp/retriage/batch_NN.csv`.
- Aggregate. Compute `deployable` myself (lowest released X.Y.Z fixVersion → buggy patch-1).
- bugs.txt = is_db_behavior AND category in {cql-semantics, storage-engine, distributed-multinode,
  other-db-behavior}. Exclude ci-test-infra, internal-tooling, test-logic-only.

## Phase 2 — Reproduce (in kind)
Priority order: (a) all deployable cql-semantics, (b) storage-engine, (c) distributed-multinode,
(d) other-db-behavior. "Deployable" = buggy version is a released X.Y.Z with an official image.
- Fast path for released buggy versions: deploy STOCK `cassandra:<ver>` (bug already present) and
  run reproducer via cqlsh — no custom build needed (same as 20050).
- Single-node pod suffices for cql-semantics; multi-node cluster for distributed bugs.
- storage-engine: single node + `nodetool flush/compact`.
- Trunk-only bugs (fixed only in 6.0-alpha/6.0/7.x, no released image) → document as blocked.
- Track every attempt in session `repro` table; write outcomes to repro-findings.md / repro-progress.md.

## Status — Phase 1 & 2 COMPLETE; Phase 3 IN PROGRESS
- [x] Phase 1 triage (5 batches) → 51 DB-behavior bugs
- [x] bugs.txt updated (51 bugs, grouped, deployable-first)
- [x] Phase 2 reproductions: **6 reproduced with A/B controls** — 20050, 21348 (cql);
      21065, 20972 (storage); 21057 (other); 21092 (distributed).
- [x] Documented dispositions for the rest: 2 not-reproducible, 2 not-observable,
      8 blocked-hard, 1 blocked-risk (21245 shared-disk), 32 trunk-only (no image).
- [x] repro-findings.md (Parts 2–4 rewritten) + repro-progress.md updated; repro table current.

## Phase 3 — attempt the 9 previously-blocked bugs (NEW directive)
Skip not-reproducible (20915, 20982), not-observable (20917, 21389), and trunk-only (32).
Attempt the **8 blocked-hard + 1 blocked-risk**. See repro-progress.md "Follow-up (Phase 3)" table.
- [~] 21245 (storage, 5.0.8): ATTEMPTING via `max_space_usable_for_compactions_in_percentage` lever
      (no real disk fill). Pod up, 2 sstables loaded; first `nodetool compact` did NOT trip the error.
      NEXT: re-verify 5.0.8 `getExpectedCompactedFileSize` returns uncompressed; recompute lever
      (lower pct or bump `min_free_space_per_drive`); re-trigger; capture expected-write-size.
- [ ] 21219 (5.0.6): mTLS MutualTlsAuthenticator + cert + `ADD IDENTITY` superuser bind (CVE-2026-27314)
- [ ] 20871 (4.0.19): repaired-data tracking + counter cells in repaired sstables → read AIOOBE
- [ ] 21132 (5.0.6): 3-node ring + many SAI indexes → gossip index-status startup AssertionError
- [ ] 20877 (4.0.19): ≥2-node ring + incremental repair + bootstrap → uncleaned FINALIZED system.repairs
- [ ] 21428 (4.0.20): multi-node + transient partition → ECHO_REQ timeout → node stuck DOWN
- [ ] 21332 (5.0.8): static SAI + range-tombstone RFP resurrection (likely in-JVM-dtest-only; confirm)
- [ ] 21290 (4.1.11): empty heartbeat file on crash-between-create-and-write (likely non-determ.; confirm)
- [ ] 20976 (5.0.5): BTI sstable token-range AssertionError (Jira = mailing-list link only; confirm)

## Notes
- 21245 lever approach replaces the deferred shared-disk fill (no longer fills the fs).
- Fixed-image controls: 21219→5.0.7, 20871→4.0.20, 21132→5.0.7, 20877→4.0.20.
  No image for 21245/21428 fixes (5.0.9/4.0.21/4.1.12 unreleased) → within-version evidence.
- Stock repro pods still running (namespaces in repro-progress.md). `repro_helper.sh` in session files.
