# Benchmark Problems from reproduced Cassandra bugs — progress & TODO

Date: 2026-06-12. Goal: update the `sregym-db-bug-reproduction` skill so every reproduced bug becomes a
benchmark Problem, then implement Problems for the reproduced Cassandra bugs. Both phases used workflows.

## Done

### Phase 1 — skill updated (workflow `wf_cec53f84`)
- Added section **"## From a reproduced bug to a benchmark Problem"** to
  `.claude/skills/sregym-db-bug-reproduction/SKILL.md` (update agent + review agent; review = accurate).
- It documents: use the per-bug evidence log `.claude/repro-evidence/repro-CASSANDRA-<n>.md` as the
  authoritative source; the two base classes (GenericCustomBuildProblem vs CassandraBugProblem) and when to
  use each; a **reproduction-shape decision tree** (single-node CQL / wrong-result / config-gated /
  nodetool-sequence / multi-node / cross-version → how to encode each, stub the un-encodable ones); required
  fields; oracle semantics; registration; and a **verify-statically-only** rule (never instantiate).
- Enabling change: exposed `prebuilt_from_stock` as a per-problem override on `GenericCustomBuildProblem`
  (`generic_custom_build.py`) so stock-image bugs deploy the stock buggy image instead of a ~30-min source
  build. Also fixed two stale `expected_output` code comments (said "correct value"; runtime uses the
  **buggy** value) in `generic_custom_build.py` and `db_build_spec.py`, and set `prebuilt_from_stock=True`
  on the `auto_cassandra_20050.py` example.

### Phase 2 — Problems implemented (workflows `wf_40280171`, `wf_e55c683a`, `wf_092a7319`)
- **84 new `auto_cassandra_<n>.py` Problem files** generated, one per reproduced bug with a usable evidence
  log (fan-out: one agent per bug, read the skill + its evidence log, classify shape, write the file,
  `py_compile`). All are `GenericCustomBuildProblem` subclasses with `prebuilt_from_stock=True`, so they are
  **auto-discovered** by `ProblemRegistry` (no registry edit). Covers all reproduced bugs with usable
  evidence: the candidate-batch reproductions plus the 10 Part-3 reproductions (for the 5 Part-3 bugs that had
  no per-bug log — 21348, 21065, 20972, 21057, 21092 — evidence logs were first extracted from
  `repro-findings.md` Part 3 into `.claude/repro-evidence/`). Excluded: 20108/18105/16086/18108/20050 (already
  have Problems), 19166 (inconclusive), 14113 (safeguard).
- Breakdown (85 `auto_cassandra_*` files total, incl. the pre-existing 20050):
  - ~63 runnable encodings: single-node CQL, wrong-result (with buggy `expected_output`), config-gated
    (CR `cassandraYaml` patch / `crash_on_startup`), and nodetool-sequence (custom `inject_fault`).
  - 17 multi-node / cross-version **STUBS** (clearly marked; full steps preserved in the docstring +
    `reproducer`; `continuous_reproducer=False` so no false mitigation oracle).
  - 29 with `continuous_reproducer=True` (mitigation oracle); 6 `crash_on_startup=True`.

### Verification (static only — per skill rule, nothing was instantiated/deployed)
- `uv run python -m py_compile` on all 85 files: **all compile**.
- `ProblemRegistry()` loads all **85** `auto_cassandra_*` problems as classes (total registry 144 → 228);
  loader stores classes, does not call `__init__`, so no build/deploy was triggered.
- Fidelity spot-check (19475, 15814, 20171, 14204, 21332): buggy versions match the logs (14204 correctly
  trusted the log's `4.1.1` over the `4.1.2` hint), reproducers encode the buggy path (not the A/B control),
  `expected_output` = the buggy value only for wrong-result bugs.

## TODO / open items

1. **Runtime validation (the big one).** No generated Problem has been deployed/run — verification was
   static only (the prior reproduction phase exhausted the 63 GiB disk). Each Problem still needs a real
   `Conductor` run to confirm it deploys the stock buggy cluster and that the reproducer fires the bug.
   Blocked on disk/time; do it in batches with teardown+prune between, or on a larger host.
2. **17 multi-node / cross-version stubs** (e.g. 20877, 21132, 21332, 16146, 16156, 16718, 16796, 16418,
   14463, 14559, …): need a multi-node deploy path (ring + per-replica/gossip orchestration, scale/bootstrap,
   or two clusters for sstableloader) before they reproduce. Steps are recorded in each file.
3. **15 auth-gated bugs** (12525, 12949, 16372, 16902, 16977, 17415, 17623, 19401, 19749, 19880, 20052,
   20086, 20171, 21219, …): the shared continuous-reproducer probe connects with bare `cqlsh` (no `-u/-p`),
   so under `PasswordAuthenticator` the mitigation pod cannot authenticate. Most are wired diagnosis-only.
   Framework fix needed: a **credentialed reproducer probe** (read the `<cluster>-superuser` secret) for
   mitigation grading on auth bugs.
4. **Best-effort `root_cause_file`** on a few bugs where the evidence log named no source file and no source
   was cloned (e.g. 16902, 21332 inferred; 19475/19566/18647/17467/20171 confirmed via WebFetch of the fix
   tag). Re-verify against the buggy git ref when source is available.
5. **Not implemented:** 19166 (reproduction log incomplete — downgraded to inconclusive) and 14113 (the
   reproduction agent repeatedly tripped a Claude cyber-safeguard false-positive). Both need re-reproduction
   first.
6. **Carried over from the reproduction phase:** 23 candidates still deferred — 11 slow medium-confidence
   multi-node rings + the 12 needs-fix-test appendix (`repro-findings.md` Part 6). Resumable via
   `.claude/repro_candidates_workflow.js`.
7. **Untracked artifacts.** `.claude/` (evidence logs, `candidate_results.json`, workflow scripts) and the new
   `auto_cassandra_*.py` files are not committed. A `git commit` would harden them. (Not committed — by policy.)

## Pointers
- Problem files: `sregym/conductor/problems/auto_cassandra_<n>.py` (auto-discovered).
- Skill: `.claude/skills/sregym-db-bug-reproduction/SKILL.md` → "From a reproduced bug to a benchmark Problem".
- Templates referenced: `auto_cassandra_20050.py` (simple), `cassandra_20108.py` / `cassandra_18105.py`
  (custom inject_fault), `generic_custom_build.py` (base, `prebuilt_from_stock`).
- Evidence logs: `.claude/repro-evidence/repro-CASSANDRA-<n>.md`; results: `candidate_results.json`.
- Reproduction record: `repro-findings.md` (Parts 3-6), `repro-progress.md`.
- Workflow scripts: `.claude/skill_update_workflow.js`, `.claude/problem_gen_workflow.js`,
  `.claude/repro_candidates_workflow.js`, `.claude/classify_workflow.js`.
