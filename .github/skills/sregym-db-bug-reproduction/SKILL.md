---
name: sregym-db-bug-reproduction
description: >-
  Guide to SREGym's automatic problem-construction pipeline, which turns a database bug report
  (GitHub or Jira issue) into a runnable SREGym problem. The pipeline resolves the buggy version,
  clones and builds a custom database image, deploys it on a kind cluster, swaps in the buggy image
  to inject the fault, runs a reproducer, and evaluates the agent's fix with oracles. Use this skill
  when the user wants to auto-generate or hand-craft an SREGym problem from a database issue,
  reproduce a database bug (Cassandra, TiDB, MongoDB, CockroachDB, or etcd), add a new database to
  DB_REGISTRY, or debug any stage of the issue-parsing, reproducer-extraction, image-build, deploy,
  fault-injection, or oracle-evaluation flow.
user-invocable: true
---

# Reproducing database bugs as SREGym problems

SREGym can take a public database bug report and automatically construct a benchmark "problem":
it deploys a real database cluster running the *buggy* build, triggers the bug, and grades whether
an agent diagnoses the root cause and mitigates it. This skill explains how that pipeline works and
how to operate, extend, and debug it.

## Mental model: two phases

1. **Construction** (issue URL → `auto_<db>_<number>.py`): parse the issue, find the buggy version
   and git ref, extract a runnable reproducer, optionally validate it, then render a problem file.
   Driven by `ProblemGenerator`.
2. **Runtime** (problem → graded run): clone source at the buggy ref, build a custom Docker image,
   deploy the *stock* cluster, swap in the buggy image to inject the fault, run the reproducer, and
   evaluate with oracles. Driven by `GenericCustomBuildProblem` + `GenericDBApplication`, orchestrated
   by the `Conductor`.

A generated problem is just a small subclass of `GenericCustomBuildProblem`. The heavy lifting is
shared infrastructure keyed off `db_name` via `DB_REGISTRY`.

## Quick start

### Auto-generate a problem from an issue URL

```python
from sregym.conductor.problems.problem_generator import ProblemGenerator

# Writes sregym/conductor/problems/auto_tidb_67650.py and returns "auto_tidb_67650".
problem_id = ProblemGenerator.generate("https://github.com/pingcap/tidb/issues/67650")
```

`generate()` is idempotent on the output path — re-running the same URL overwrites the file
(`problem_generator.py`, `ProblemGenerator.generate`). The new file is auto-discovered by
`ProblemRegistry` on next instantiation, so it can be run immediately by the Conductor.

Helpful environment variables before generating:

| Variable | Effect |
| --- | --- |
| `GITHUB_TOKEN` | Authenticated GitHub API calls (5000 vs 60 req/hr). Read in `GitHubIssueParser.__init__`. |
| `ANTHROPIC_API_KEY` | Enables LLM reproducer extraction/repair; without it, extraction falls back to regex only. |
| `JIRA_TOKEN` / `JIRA_EMAIL` / `JIRA_API_TOKEN` | Jira auth (Bearer or Basic) in `JiraIssueParser`. |
| `SREGYM_SKIP_REPRODUCER_VALIDATION=1` | Downgrades validation failures from hard errors to warnings (`validation_required()` in `reproducer_validator.py`). |

## Phase 1 — Construction (issue → `auto_*.py`)

Entry point: `ProblemGenerator.generate()` in `sregym/conductor/problems/problem_generator.py`.

1. **Route the URL.** `sregym/service/issue_parser.py:parse_issue()` dispatches by substring:
   `"github.com"` → `GitHubIssueParser`, else `"/browse/"` → `JiraIssueParser`, else `ValueError`.
2. **Resolve DB + version + ref.** `GitHubIssueParser.resolve()` (`sregym/service/github_issue_parser.py`)
   matches the repo to a `DBBuildSpec` (via `github_repo`), then finds the buggy ref through a fallback
   chain: commit SHA in body → SHA in timeline → version label → milestone → semver in title/body.
   Returns a `ParsedIssue`.
3. **Extract the reproducer.** `sregym/service/reproducer_extractor.py:extract_reproducer_full(body)`
   returns a **7-tuple** in this exact order:
   `(reproducer, expected_output, buggy_output, correct_output, setup_preconditions, crash_on_startup, fault_injection_type)`.
   It tries the LLM extractor first, then a regex fallback only if no reproducer and not a startup crash.
   It returns nothing (or discards the candidate) for Sentry-auto-filed issues, stack-trace/panic-only
   bodies, prose-only blocks, redacted SQL, and cases where `buggy_output` appears verbatim inside the
   reproducer (a circular match that usually means a traceback was captured by mistake).
4. **Validate (best-effort).** `sregym/service/reproducer_validator.py:validate_reproducer()` runs the
   reproducer in a throwaway Docker container of the *stock* image and compares output to
   `buggy_output`/`correct_output`. **Real validators exist only for `mongodb` and `cockroachdb`**; every
   other DB (and any case where Docker/image is unavailable) is `skipped` (inconclusive). On failure the
   generator asks the LLM to repair the reproducer and re-validates, up to `_MAX_REPAIR_ATTEMPTS = 2`.
   A hard failure aborts generation unless `SREGYM_SKIP_REPRODUCER_VALIDATION=1`.
5. **Render the file.** `ProblemGenerator._render()` emits a `GenericCustomBuildProblem` subclass.
   - Normal reproducer → `reproducer`, `continuous_reproducer`, optional `expected_output` and `_setup_preconditions_sql`.
   - `crash_on_startup` → `crash_on_startup = True` + a stub `setup_preconditions()` to fill in.
   - `fault_injection_type == "node_kill"` → an `inject_fault()` override that runs the reproducer in the
     background, kills a random DB pod, then joins.
   - `buggy_output`/`correct_output` are **validation-only** and never written to the file.
   Naming: `problem_id = auto_{db}_{number}`, class `Auto{Db}{number}` (GitHub `/issues/N`, Jira `/browse/PROJECT-N`).

## Phase 2 — Runtime (build → deploy → inject → evaluate)

Base class: `GenericCustomBuildProblem` in `sregym/conductor/problems/generic_custom_build.py`.

1. **`__init__`** looks up `DB_REGISTRY[db_name]`, applies per-problem `build_cmd`/`build_image`
   overrides, resolves `(version, git_ref)`, clones source via
   `sregym/service/source_manager.py:SourceManager.ensure_source()` (cached under `/tmp/sregym-sources`,
   shallow clone with a fetch+checkout fallback for commit SHAs), then builds the custom image with
   `sregym/service/generic_db_build_manager.py:GenericDBBuildManager`.
2. **Build image.** `build_from_directory()` (or `build_with_patches()` for hand-crafted patches) hashes
   the source/patch tree for cache-keyed reuse. For Go projects, `_resolve_build_image()` reads `go.mod`
   and overrides the toolchain image. `prebuilt_from_stock=True` skips compilation and just re-tags the
   stock base image (used when the bug already ships in the public image). The image is loaded into the
   cluster via a `kind load` → SSH → privileged DaemonSet fallback chain.
3. **Deploy stock cluster.** The Conductor calls `problem.app.deploy()` during setup
   (`conductor.py:deploy_app`). `GenericDBApplication.deploy()` installs the operator/Helm chart and waits
   for the cluster to be Ready. Two deploy modes (see `helm_deploy_chart`): operator + CR, or the Helm
   release *is* the cluster (no CR).
4. **Inject the fault.** The Conductor calls `inject_fault()` exactly once before the first agent stage
   (`conductor.py:_advance_to_next_stage`, when `start_index == 0 and not fault_injected`).
   `inject_fault()` swaps the running cluster to the buggy image (`inject_buggy_image()`, with an
   operator-override fallback that scales the operator to 0 and patches StatefulSets directly), runs
   `setup_preconditions()`, runs the reproducer, and — if `continuous_reproducer` — deploys a looping
   reproducer Deployment. `crash_on_startup` problems instead use `inject_buggy_image_expect_crash()` and
   wait for CrashLoopBackOff.
5. **Evaluate / recover.** Mitigation is graded by an oracle (see below). `recover_fault()` restores the
   stock image.

## Problem class anatomy (`GenericCustomBuildProblem`)

**Required:** `db_name` (key into `DB_REGISTRY`), plus **either** `issue_url` (auto mode) **or** both
`db_version` and `source_git_ref` (hand-crafted mode).

**Optional overrides** (all defined near the top of `generic_custom_build.py`):
`root_cause_description`, `root_cause_file`, `reproducer`, `expected_output`, `continuous_reproducer`,
`crash_on_startup`, `_setup_preconditions_sql`, `extra_helm_args`, `build_cmd`, `build_image`,
`patch_dir` (hand-crafted patch overlay).

### Hand-crafting a problem (when auto-generation can't reproduce it)

Set `db_version` + `source_git_ref` instead of `issue_url`, write the `reproducer` explicitly, and pick
the oracle behavior with `continuous_reproducer` / `expected_output` / `crash_on_startup`. See
`sregym/conductor/problems/auto_etcd_18089.py` for a clean hand-crafted example (deterministic logic bug
with a continuous reproducer), and `auto_zookeeper_2213.py` for a stub. For source-patch bugs, point
`patch_dir` at a directory of files to overlay before building.

## Adding a new database

Add a `DBBuildSpec` entry to `DB_REGISTRY` in `sregym/service/db_build_spec.py` describing the four phases
(Source / Build / Package / Deploy) and supply a `run_reproducer_fn` (and optional
`reproducer_workload_fn` for continuous reproducers). See `reference.md` for the full field list and a
per-DB cheat-sheet. After editing, **always** confirm the module imports — a syntax error here silently
breaks auto-loading of *every* generated problem (see Known bugs).

## Oracles & readiness semantics

Continuous-reproducer problems use `sregym/conductor/oracles/reproducer_pod_mitigation.py:ReproducerPodMitigationOracle`,
which reads the reproducer pod's readiness. The mapping is controlled by `expect_unready`, which
`generic_custom_build.py` sets to `expected_output is not None`:

- **Wrong-result bugs** (`expected_output` set → `expect_unready=True`): the probe greps for the buggy
  value, so **Ready = bug present, NotReady = fixed**.
- **Crash/error bugs** (no `expected_output` → `expect_unready=False`): the probe checks the exit code, so
  **NotReady = bug present, Ready = fixed**.

## Known bugs & gotchas

- **Jira parser unpack mismatch.** `extract_reproducer_full()` returns 7 values, but
  `JiraIssueParser.resolve()` (`sregym/service/jira_issue_parser.py:56-58`) unpacks only 5 — this raises a
  `ValueError` at runtime and also drops `setup_preconditions`/`fault_injection_type`. GitHub issues work;
  Jira issues are currently broken until this is fixed.
- **Registry typo.** `sregym/conductor/problems/registry.py:279` registers `"auto_tidv_67002"` (should be
  `"auto_tidb_67002"`).
- **Auto-load is all-or-nothing on shared imports.** `ProblemRegistry._load_auto_generated()` catches
  per-file import errors, but a syntax/import error in a *shared* dependency like `db_build_spec.py` breaks
  loading for all generated problems. Compile-check after editing shared modules:
  `uv run python -m py_compile sregym/service/db_build_spec.py`.
- **`run_reproducer_background()` annotation is wrong.** It returns a `threading.Thread`, not a
  `subprocess.Popen` (`generic_db_app.py`). Generated `node_kill` injectors call `t.join()`, so runtime is
  fine, but the type hint/docs are misleading.
- **Reproducer validation is narrow.** Only `mongodb` and `cockroachdb` are actually validated before a
  file is written; for other DBs a "successful" generation does not mean the reproducer fires the bug.
- **Distroless DB images** (e.g. `quay.io/coreos/etcd`) have no shell — reproducers run from a separate
  client pod (etcd uses an `alpine:3.20` pod that downloads `etcdctl`).

## Key files

| File | Role |
| --- | --- |
| `sregym/conductor/problems/problem_generator.py` | Issue → `auto_*.py` generator + validation/repair loop. |
| `sregym/service/issue_parser.py` | Routes a URL to the GitHub or Jira parser. |
| `sregym/service/github_issue_parser.py` / `jira_issue_parser.py` | Resolve DB spec, version, git ref; build `ParsedIssue`. |
| `sregym/service/reproducer_extractor.py` | LLM+regex reproducer extraction with safety guards. |
| `sregym/service/reproducer_validator.py` | Docker-based reproducer validation (mongodb, cockroachdb). |
| `sregym/conductor/problems/generic_custom_build.py` | Base problem class; build/deploy/inject/recover lifecycle. |
| `sregym/service/generic_db_build_manager.py` | Builds/re-tags custom images and loads them into the cluster. |
| `sregym/service/apps/generic_db_app.py` | Deploys clusters, swaps images, runs reproducers, cleans up. |
| `sregym/service/source_manager.py` | Clones/caches source at a specific git ref. |
| `sregym/service/db_build_spec.py` | `DBBuildSpec` schema + `DB_REGISTRY` + per-DB reproducer helpers. |
| `sregym/conductor/oracles/reproducer_pod_mitigation.py` | Readiness-based mitigation oracle for continuous reproducers. |
| `sregym/conductor/problems/registry.py` | Static registry + `_load_auto_generated()`. |

## Reference

See `reference.md` (next to this file) for the full `DBBuildSpec` field list grouped by phase and a
per-database cheat-sheet (build mode, deploy mode, client image, and reproducer command).
