# Findings — reproducing Apache Cassandra bugs and turning them into SREGym benchmark problems

Consolidated findings from the end-to-end effort. Detailed records: `repro-findings.md` (per-bug
reproductions, Parts 1-6), `repro-progress.md` (reproduction tracking), `benchmark-problems-progress.md`
(Problem implementation + TODO). Full process narrative: `cassandra-bug-reproduction-report.md`.

## Headline numbers

| Stage | Result |
| --- | --- |
| Original `bugs.txt` (51 Cassandra DB-behavior bugs) | 11 reproduced; 2 not-reproducible; 2 not-observable; 4 confirmed-blocked; 32 trunk-only (no released image) |
| `bugs.txt` re-curated + 100 new candidates collected | Jira sweep 1800 → 671 deployable → 433 pre-filtered → 174 stageable → 100 selected (text-triaged, tiered) |
| New-candidate reproduction (87 attempted of 98 reproducible) | **74 reproduced** (66 verbatim-verified, 8 hedged); 6 not-reproducible; 3 confirmed-blocked; 2 needs-fix-test; 2 inconclusive |
| Deferred (disk + time wall) | 11 multi-node rings + 12 needs-fix-test appendix |
| **Distinct bugs reproduced (both passes)** | **85** (verified floor 77 = 11 + 66) |
| Benchmark Problems implemented | **85 `auto_cassandra_*.py`** (84 new + the pre-existing 20050), all compile, all auto-load in `ProblemRegistry` (registry 144 → 228) |

## What is and is not reproducible (Cassandra, stock-image path)

- **Reproducible from a stock image:** a bug fixed in a released `X.Y.Z` patch is present in the
  `cassandra:<X.Y.(Z-1)>` image, so a single stock pod (or a multi-node ring) reproduces it with no source
  build. This "fast path" carried every reproduction here. Released-image ceilings (Docker Hub `library/cassandra`):
  **3.11→19, 4.0→20, 4.1→11, 5.0→8**. There are no `5.0.9 / 4.0.21 / 4.1.12 / 6.x / 7.x` tags.
- **Not reproducible via stock image (blocked-no-image):** bugs fixed only in `6.0-alpha*/6.0/6.x/7.x`
  (trunk). 32 of the original 51 were trunk-only. A source build is required and was out of scope.
- **Not-reproducible (path shadowed):** the buggy code is unreachable from a client because earlier
  validation or a disabled feature intercepts it (e.g. 20915 keyspace-name length, 20982 ALTER TYPE).
- **Not-observable:** the fix is an internal refinement with no client/operator-visible change (error-type
  swap, validation hardening) — e.g. 20917, 21389. These make poor benchmark problems and were excluded.
- **Confirmed-blocked (un-stageable mechanism):** the reproducer needs infrastructure that cannot be staged
  with `kubectl exec` on stock pods — in-JVM message interception / `executeInternal` (20871, 12126), a
  precise timing/partition window (21428), a crash between two syscalls, or no concrete reproducer in the
  issue (20976 = mailing-list link only). Name the specific un-stageable mechanism; this is a clean outcome.
- **blocked-disk-constrained:** the reproducer inherently needs more data than the host disk budget
  (multi-GiB sstables, heavy stress writes, >2 GiB indexes). Record rather than fill the disk.

## Techniques that worked

- **Single-node pod** for CQL-semantics and most storage bugs; append a `cassandra.yaml` block in the pod
  command for config-gated bugs; `nodetool disableautocompaction` + repeated `flush` to retain ≥2 sstables
  for compaction-iteration bugs.
- **Multi-node ring** as a headless Service + StatefulSet (`podManagementPolicy: OrderedReady`, seed =
  `cass-0`). For per-replica divergence that normally needs an in-JVM dtest, use **gossip isolation**:
  `nodetool disablegossip` on the peers, confirm `DN`, write at `CONSISTENCY ONE`, `nodetool flush`,
  re-enable gossip; verify the physical divergence with `sstabledump` on each node's local `Data.db` (a
  `CL=ONE` read routes to an arbitrary replica and is not a reliable per-node probe). This reproduced
  CASSANDRA-21332, which the first pass had judged "likely in-JVM-dtest-only."
- **Evidence bar:** a reproduction needs a **verbatim** buggy signature (exact exception + frame, server
  error, or wrong-result row) and, where a fixed image exists, an **A/B control** on the fixed version.
- **Three prior "blocked-hard" verdicts were overturned with evidence** once a 4-node cluster was available:
  21219 (privilege escalation needs no mTLS PKI), 21332 (stageable on a real ring), 21290 (the deterministic
  manifestation, not the crash race).

## Meta-findings (tooling, infrastructure, methodology)

- **Text-triage filtering is high-value.** Naive "DB-behavior" filtering reproduced ~22% (11/51). After
  classifying candidates by observable-symptom + reproducer-provenance + reproduction-shape, the new batch
  reproduced ~76% (74/98). A 25-agent classifier over 433 candidates fed the selection.
- **Disk is the binding constraint.** On a 63 GiB host the kind cluster filled three times during the
  reproduction phase. The fix: bounded concurrency (worker pool, 3-8), `kubectl delete ns` +
  `crictl rmi --prune` between waves, and tearing down idle clusters. Image accumulation in the kind node
  containerds (not pod data) was the main hog; pruning reclaimed ~24 GiB at a stroke.
- **Cyber-safeguard false-positives** fired on security-flavored bugs (CVE/auth) — 20976 and 14113 had
  their reproduction agents blocked mid-run. Assess those read-only by hand; record as inconclusive.
- **SREGym tooling findings (record-only, mostly already addressed):** the Jira parser 7-tuple/version
  issues (fixed in an earlier session, Part 5); two stale `expected_output` code comments that described the
  inverse oracle convention (fixed this session); `GenericCustomBuildProblem` lacked a per-problem
  `prebuilt_from_stock` override for stock-image bugs (added this session).
- **Workflow orchestration:** `args` delivery to workflow scripts was unreliable in this harness — pass
  batches via a file + a trivial reader agent. A worker-pool (bounded concurrency, no chunk barrier) beat
  both unbounded `parallel` (disk crises) and chunked `parallel` (a slow agent blocks the whole chunk).
  Idempotent/resumable runs via per-item done-markers (`/tmp/repro-<key>.md`). Verify code-gen statically
  (`py_compile` + registry loads); never instantiate a Problem (that triggers a build/deploy).

## Benchmark-problem implementation findings

- Reproduced bugs map cleanly to `GenericCustomBuildProblem` subclasses named `auto_cassandra_<n>.py`
  (auto-discovered, no registry edit) with `prebuilt_from_stock=True` so they deploy the stock buggy image
  instead of a ~30-min `ant jar` build.
- The reproduction shape determines the encoding: single-node CQL → `reproducer` string; wrong-result →
  also set `expected_output` to the **buggy** value (the mitigation oracle greps for it: Ready = bug
  present); config-gated → `setup_preconditions()` / CR `cassandraYaml` patch / `crash_on_startup`;
  nodetool-sequence → custom `inject_fault()`; multi-node / cross-version → a **clearly-marked stub** with
  the full steps preserved (flattening a multi-node repro into one CQL compiles and registers but silently
  does not reproduce).
- **Open limitations (see `benchmark-problems-progress.md` TODO):** runtime validation is pending (static
  verification only; disk-bound); 17 multi-node/cross-version stubs need a ring deploy harness; 15
  auth-gated problems need a credentialed reproducer probe (the shared probe uses bare `cqlsh`); some
  `root_cause_file`s are best-effort.
