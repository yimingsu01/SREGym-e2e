# Reproducing Apache Cassandra bugs and implementing them as SREGym benchmark problems

A full process report. Companion documents: `FINDINGS.md` (distilled findings), `repro-findings.md`
(per-bug detail, Parts 1-6), `repro-progress.md` (reproduction tracking), `benchmark-problems-progress.md`
(implementation status + TODO), `.claude/repro-evidence/` (per-bug evidence logs + `candidate_results.json`).

---

## 1. Objective

Take real, fixed Apache Cassandra bug reports, reproduce them in a live Kubernetes (kind) cluster, and turn
each reproduced bug into a runnable SREGym benchmark problem (where an SRE agent must diagnose the root
cause and/or mitigate the fault). The work ran in phases driven by the `sregym-db-bug-reproduction` skill
and multi-agent workflows.

The end state: **85 distinct Cassandra bugs reproduced** in kind (verified floor 77) and **85 benchmark
Problem files** implemented and auto-registered, with a documented, resumable tail of deferred work.

---

## 2. Part A — Reproducing the bugs

### 2.1 The core insight: the stock-image fast path

Cassandra publishes a Docker image per released patch. A bug fixed in `X.Y.Z` is, by definition, present
in `X.Y.(Z-1)`, and the official `cassandra:<X.Y.(Z-1)>` image already contains the buggy code. So a bug
can be reproduced by deploying that stock image and running the reproducer — **no source build required**.
The buggy version is simply `fix patch - 1`. Where the fix patch also has a released image, running the
identical workload on the fixed version is a clean A/B control.

This path has a hard ceiling: the highest released image per series. Observed on Docker Hub
`library/cassandra`: **3.11→19, 4.0→20, 4.1→11, 5.0→8**, with no `5.0.9 / 4.0.21 / 4.1.12 / 6.x / 7.x`
tags. Bugs fixed only in trunk (`6.0-alpha*/6.0/6.x/7.x`) have no released image and are not reproducible
by this path ("blocked-no-image"); they would need a source build.

### 2.2 Bug sourcing and triage

- The original `bugs.txt` was a 51-bug Cassandra "DB-behavior" subset (from 100 cached Jira issues),
  grouped by category, with deployable bugs marked. Of these, the first pass reproduced **11**, classified
  2 not-reproducible, 2 not-observable, 4 confirmed-blocked, and 32 trunk-only (no image).
- `bugs.txt` was then re-curated: pruned to the 11 reproduced, and extended with **100 freshly-collected
  candidates** chosen to be *likely reproducible* using what the first pass taught.

### 2.3 Collecting 100 new candidates (a classification workflow)

1. **Jira sweep.** Queried the Apache Jira REST API for the most recent ~1800 fixed `CASSANDRA` bugs.
2. **Deployability filter (programmatic).** Kept bugs with a released fix patch whose `patch-1` image
   exists (within the ceilings), excluding the 100 already cached → **671 deployable**.
3. **Component pre-filter.** Dropped test-infra/build/docs components → **433 candidates**, full Jira
   bodies fetched to `/tmp/jira_new/`.
4. **Classification workflow** (25 parallel agents over the 433). Each agent classified a bug on:
   `db_behavior`, an **observable-symptom gate** (names the client/operator-visible effect, or `none` —
   this catches the "internal refinement" non-bugs), a **reproducer-provenance** field
   (`in-body` / `derivable` / `linked-test-only` / `absent`), a **reproduction-shape**
   (single-node / multi-node-stageable / in-jvm-dtest-only / timing-partition / crash-window), category,
   buggy version, and confidence. Result: **174 recommended** (stageable + observable + in-body/derivable).
5. **Selection.** Took **100**, ~25 per category, high-confidence first (58 high + 42 medium), plus a
   12-item "needs-fix-test" appendix. A 5-pick spot-check against the bodies confirmed no classifier
   inflation. The result was written to `bugs.txt` as a tiered, annotated, UNVERIFIED candidate list.

This filtering was the highest-leverage step: the naive first pass reproduced ~22% (11/51), while the
filtered batch reproduced ~76% (74/98).

### 2.4 Reproduction methodology

- **Single-node pod** (stock `cassandra:<buggy>`, heap capped ~1 GiB) for CQL-semantics and most storage
  bugs. Drive the reproducer via `kubectl exec ... cqlsh`/`nodetool`. Config-gated bugs append a
  `cassandra.yaml` block in the pod command before `docker-entrypoint.sh cassandra -f`. Compaction-iteration
  bugs use `nodetool disableautocompaction` + repeated `flush` to retain ≥2 sstables.
- **Multi-node ring** for distributed bugs: a headless `Service` + `StatefulSet`
  (`podManagementPolicy: OrderedReady`, seed `cass-0`, `GossipingPropertyFileSnitch`), verified with
  `nodetool status` (N × `UN`). For **per-replica divergence** that normally requires an in-JVM dtest, use
  **gossip isolation**: `nodetool disablegossip` on the other nodes, confirm they show `DN`, write at
  `CONSISTENCY ONE` with explicit `USING TIMESTAMP`, `nodetool flush`, then re-enable gossip; verify the
  physical divergence with `sstabledump` on each node's local `Data.db` (a `CL=ONE` read is routed to an
  arbitrary replica and is not a reliable per-node probe).
- **Evidence bar.** A reproduction requires a **verbatim** buggy signature (exact exception class + frame,
  server error message, or the concrete wrong-result row) and, where a fixed image exists, an **A/B
  control** showing the fixed version does not misbehave. Dispositions: `reproduced`, `not-reproducible`
  (path shadowed), `not-observable` (internal refinement), `confirmed-blocked` (name the un-stageable
  mechanism), `blocked-disk-constrained`, `needs-fix-test`, `blocked-no-image` (trunk-only).
- **Notable reproductions.** Examples that exercised the full method: 21332 (SAI static-column reads
  resurrect range-tombstoned data — reproduced on a real 3-node ring via gossip isolation, overturning a
  "dtest-only" verdict); 20877 (FINALIZED repair sessions not cleaned after range movement — differential
  on the coordinator node across a 2→3 scale); 21132 (324 SAI indexes + cold restart → legacy INDEX_STATUS
  gossip overflows `Short.MAX_VALUE` → `AssertionError`, join deadlock); 21219 (CVE-2026-27314 privilege
  escalation — reproduced under plain `PasswordAuthenticator`, no mTLS PKI needed).

### 2.5 Running reproductions at scale (orchestration + the disk fight)

The candidate reproductions were fanned out with a parameterized, resumable workflow
(`.claude/repro_candidates_workflow.js`): a gated preflight, then one agent per bug. Each agent read the
bug's cached Jira JSON, deployed the stock buggy pod/ring, ran the reproducer, captured the verbatim
signature, ran the A/B control, wrote an evidence log to `/tmp/repro-CASSANDRA-<n>.md`, and tore down its
namespace.

Several hard problems shaped the final design:

- **Pilot gate.** The first fan-out was gated on a 10-agent pilot. The pilot caught that workflow `args`
  were not being delivered to the script (everything fell back to defaults), and that some agents
  paraphrased the structured `verbatim_signature` field while the log held the real evidence. Cost of the
  gate: ~600 k tokens instead of a 6 M-token mistake.
- **`args` delivery is unreliable in this harness.** Fix: write the batch to `/tmp/repro_batch.json` and
  have a trivial reader agent return it. This is deterministic and avoids inlining large arrays.
- **Disk is the binding constraint.** On the 63 GiB host the kind cluster hit `ENOSPC` three times. The
  hog was accumulated Cassandra images inside the kind nodes' containerds (not pod data); `crictl rmi
  --prune` reclaimed ~24 GiB at once. The durable fix: bounded concurrency + `kubectl delete ns` +
  `crictl rmi --prune` between waves, and tearing down idle clusters (the first-session version pods and a
  3-node K8ssandra cluster were removed to reclaim space; those reproductions were already documented).
- **Concurrency model.** Unbounded `parallel` (cap 16) caused disk crises; chunked `parallel` let a single
  slow agent block the whole chunk barrier. The final design is a **worker pool** (bounded concurrency,
  3-8, no barrier) so a slow agent occupies only its own slot.
- **Cyber-safeguard false-positives.** Security-flavored bugs (CVE/auth) tripped a usage-policy safeguard
  that killed the agent mid-run (20976, 14113). These were assessed read-only by hand and recorded as
  inconclusive.
- **Checkpoint discipline.** Results were banked to an accumulator and the markdown docs between waves, so
  a crash or disk event never lost completed work.

### 2.6 Reproduction results

- Original 51: **11 reproduced**; 2 not-reproducible; 2 not-observable; 4 confirmed-blocked; 32
  trunk-only.
- New 100 candidates: **87 attempted of 98 reproducible** → **74 reproduced** (66 verbatim-verified, 8
  hedged where the field paraphrased the log), 6 not-reproducible, 3 confirmed-blocked, 2 needs-fix-test,
  2 inconclusive (safeguard / truncated log). **11 multi-node rings + 12 needs-fix-test appendix deferred**
  on a combined time + disk wall (not a correctness wall — the ring reproduce rate was ~90%).
- **85 distinct bugs reproduced; verified floor 77** (11 + 66 verbatim-verified). All evidence is in
  `.claude/repro-evidence/` and `repro-findings.md` Parts 3-6.

---

## 3. Part B — Implementing the benchmark problems

### 3.1 The mapping and the two patterns

A reproduced bug becomes a `Problem` subclass. Two base classes exist in the benchmark:

- **`GenericCustomBuildProblem`** → file `auto_<db>_<n>.py`, **auto-discovered** by `ProblemRegistry`
  (no registry edit). Gives a diagnosis oracle (LLM judge on the root cause) and, with
  `continuous_reproducer=True`, a mitigation oracle (a reproducer pod whose readiness encodes bug state).
  For a bug already in the released image, the per-problem `prebuilt_from_stock=True` makes it deploy the
  stock buggy image instead of running a ~30-min `ant jar` build.
- **`CassandraBugProblem`** → file `cassandra_<n>.py`, manual registry entry, diagnosis-only; deploys a
  stock cluster via the K8ssandra operator and runs `trigger_cql` with a custom `inject_fault()`.

The chosen pattern for the reproduced bugs was `GenericCustomBuildProblem` + `prebuilt_from_stock=True`:
it auto-registers (scales to ~80 files without an 80-entry manual registry edit), gives both oracles, and
deploys the stock buggy image with no rebuild.

### 3.2 Enabling changes

- Exposed `prebuilt_from_stock` as a per-problem override on `GenericCustomBuildProblem`
  (`generic_custom_build.py`); it forwards to the existing `DBBuildSpec` field.
- Fixed two stale code comments (`generic_custom_build.py`, `db_build_spec.py`) that described
  `expected_output` as the *correct* value — runtime uses the **buggy** value (the mitigation probe greps
  for it, so Ready = bug present). An inverted `expected_output` would grade the mitigation oracle
  backwards.
- Set `prebuilt_from_stock=True` on the `auto_cassandra_20050` example so it demonstrates the pattern.

### 3.3 Skill update (workflow)

A workflow (`.claude/skill_update_workflow.js`, write agent + review agent) added the section
**"From a reproduced bug to a benchmark Problem"** to `SKILL.md`, documenting the decision tree, the two
patterns, required fields, oracle semantics, registration, and a verify-statically-only rule. The review
agent cross-checked every concrete claim against the codebase and confirmed accuracy.

### 3.4 Problem generation (workflow) and the reproduction-shape decision tree

A workflow (`.claude/problem_gen_workflow.js`) fanned out one agent per reproduced bug. Each agent read the
skill + the bug's `.claude/repro-evidence/` log (authoritative), **classified the reproduction shape**, and
emitted the matching Problem:

- **single-node CQL** → `reproducer` = the CQL block; `continuous_reproducer=True`.
- **wrong-result** → also set `expected_output` to the **buggy** value.
- **config-gated** → override `setup_preconditions()` / patch the K8ssandraCluster CR `cassandraYaml`;
  `crash_on_startup=True` for startup-failure bugs.
- **nodetool-sequence** → custom `inject_fault()` running the nodetool steps via `kubectl exec`.
- **multi-node / cross-version** → a **clearly-marked stub** (`continuous_reproducer=False` so no false
  mitigation oracle) preserving the full steps; flattening into one CQL would compile and register but
  silently not reproduce.

This was code-generation (no cluster), so it ran at full parallelism. Transient socket errors and a few
gaps were filled by re-running the idempotent workflow (it skips any bug whose file already exists). For 5
first-pass reproductions that had no per-bug log, evidence logs were first extracted from
`repro-findings.md` Part 3 so they went through the same skill-driven path.

### 3.5 Verification (static only)

Per the skill rule, nothing was instantiated or deployed (instantiation triggers a build/deploy). Checks:
`uv run python -m py_compile` on all files; `ProblemRegistry()` loads all **85** `auto_cassandra_*`
problems as classes (the loader silently skips import failures, so 85 registered proves clean imports, not
just compiles; total registry 144 → 228); a grep confirming all 12 `expected_output` values are buggy
artifacts (no oracle inversion); and a fidelity spot-check (5 across shapes + the 5 Part-3 problems)
confirming each encodes the buggy path, not the A/B control.

### 3.6 Implementation results

**85 `auto_cassandra_*.py`** problems (84 new + the pre-existing 20050), all compile, all auto-load. ~63
runnable encodings (single-node CQL, wrong-result, config-gated, nodetool-sequence) and ~17-25
clearly-marked multi-node/cross-version stubs. Excluded: already-implemented (20108/18105/16086/18108/20050),
19166 (inconclusive), 14113 (safeguard).

---

## 4. Challenges and lessons

| Challenge | Resolution |
| --- | --- |
| 63 GiB disk filled 3× during reproduction | Bounded concurrency + `kubectl delete ns` + `crictl rmi --prune` between waves; tear down idle clusters. Image accumulation (not pod data) was the hog. |
| Workflow `args` not delivered to scripts | Pass batches via a file + trivial reader agent. |
| Chunked parallel: one slow agent blocks the chunk | Worker pool (bounded concurrency, no barrier). |
| Agents over-claim "reproduced" / paraphrase signatures | Require a verbatim signature; mechanically grep the signature against the evidence log; downgrade misses. |
| Cyber-safeguard false-positives on CVE/auth bugs | Assess read-only by hand; record inconclusive. |
| Oracle inversion risk (`expected_output`) | Fixed the misleading comments; audited all `expected_output` values are buggy. |
| Multi-node bug "looks dtest-only" | Gossip isolation on a real ring reproduced it (21332). |
| Verifying ~85 generated files without deploying | Static only: py_compile + registry class-load + fidelity spot-check; never instantiate. |
| Not losing work across crashes | Checkpoint to an accumulator + the docs between waves; idempotent resumable workflows. |

---

## 5. Artifacts and how to resume

- **Problems:** `sregym/conductor/problems/auto_cassandra_<n>.py` (auto-discovered).
- **Skill:** `.claude/skills/sregym-db-bug-reproduction/SKILL.md`.
- **Evidence:** `.claude/repro-evidence/repro-CASSANDRA-<n>.md`, `candidate_results.json`.
- **Records:** `repro-findings.md`, `repro-progress.md`, `benchmark-problems-progress.md`, `FINDINGS.md`.
- **Workflows:** `.claude/{repro_candidates_workflow,classify_workflow,skill_update_workflow,problem_gen_workflow,repro_workflow}.js`.
- **Resume the deferred reproductions** (11 rings + 12 appendix): write `/tmp/repro_batch.json`
  (`{mode, ringConcurrency, candidates:[{key,buggy,topo,conf,trigger}]}`) and re-run
  `Workflow({scriptPath:'.claude/repro_candidates_workflow.js'})` — it skips any bug with a
  `/tmp/repro-<key>.md` log. A larger disk / external cluster allows higher ring concurrency.

---

## 6. Recommendations / next steps

1. **Runtime validation** of the generated Problems (the key open item): deploy each in the Conductor, in
   small batches with teardown + prune between, on a larger disk. Static verification only proves the
   files are structurally valid and faithfully encoded.
2. **A credentialed reproducer probe.** The shared continuous-reproducer probe connects with bare `cqlsh`
   (no `-u/-p`), so the 15 auth-gated problems cannot get a mitigation oracle. Reading the
   `<cluster>-superuser` secret in the probe would unblock them.
3. **A multi-node deploy path** for the ~17 ring/cross-version stubs (ring + per-replica orchestration,
   scale/bootstrap, or two clusters for sstableloader), so they become runnable rather than stubs.
4. **Finish the deferred 23 reproductions** when disk/time allow, then implement their Problems via the
   same workflow.
5. **Provision a larger disk or an external cluster** for any future at-scale reproduction; the 63 GiB host
   was the dominant constraint throughout.
