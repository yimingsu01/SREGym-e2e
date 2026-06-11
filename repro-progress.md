# Reproduction progress — bugs.txt (100 Apache Cassandra Jira issues)

Task: for every bug in `bugs.txt`, use the `sregym-db-bug-reproduction` skill to reproduce
it on the benchmark in a local **kind** cluster. A bug is only claimed "reproduced" if it is
shown failing inside the kind cluster.

> Session constraint: when a bug is spotted in the SREGym tooling itself, it is **only recorded**
> in `repro-findings.md` — it is **not** fixed in this session.

## Environment

| Component | Status |
| --- | --- |
| kind cluster (`~/kind-config.yaml`, 1 control-plane + 3 workers) | ✅ created, context `kind-kind` |
| docker / kind / kubectl / helm / uv | ✅ present (40 CPU, 157 GiB RAM, ~52 GiB disk) |
| `ANTHROPIC_API_KEY` | ❌ unset — LLM reproducer extraction disabled (regex-only fallback) |
| `JIRA_*` tokens | ❌ unset — fine, Apache Jira REST is public |
| `GITHUB_TOKEN` | ❌ unset — not needed for Jira |

## Phases

1. ✅ **Inventory + skill study.** Read the skill + pipeline source. All 100 entries are Apache
   Jira URLs (`issues.apache.org/jira/browse/CASSANDRA-*`).
2. ✅ **Fetch + triage all 100 issues** via the public Jira REST API (cached in `/tmp/jira_issues/`).
3. ✅ **Categorize reproducibility** (see `repro-findings.md`).
4. ✅ **Reproduce the viable candidate(s) end-to-end in kind.** CASSANDRA-20050 reproduced (below).
5. ✅ **Document blocking bugs + findings** in `repro-findings.md`.

## Triage outcome (why a full 100/100 auto-run is not achievable)

The skill's automatic `--create <url>` path **cannot run for these issues** (see findings: the
Jira parser is broken, and with no `ANTHROPIC_API_KEY` the reproducer extractor falls back to a
regex that only understands GitHub-markdown fences, not Jira `{code}` markup). Even setting that
aside, the issues themselves are mostly not reproducible through a deployed cqlsh cluster:

| Category | Count | Reproducible via cqlsh in kind? |
| --- | --- | --- |
| ci-test-infra (junit/CI/flaky/dtest) | 27 | No |
| other-internal | 32 | No (internal logic / unit-test observed) |
| distributed-multinode (repair/streaming/gossip/coordination) | 12 | No (need multi-node + repair/streaming) |
| internal-tooling (nodetool/metrics/virtual tables/messaging) | 8 | Rarely |
| storage-engine (compaction/sstable/memtable) | 7 | Rarely (needs flush+compaction sequencing) |
| cql-semantics | 14 | Some — best candidates |
| **Total** | **100** | |

Additional hard gate: only **36/100** were fixed in a **released** `X.Y.Z` version. The other
**64** were fixed only in `6.0-alpha*` / `6.0` / `7.x` / trunk, for which **no deployable
`k8ssandra/cass-management-api:<ver>-ubi8` base image exists** — so the buggy build cannot be
deployed by the skill's K8ssandra path at all.

Intersection (cql-semantics **and** released fix **and** an in-description CQL reproducer): **1**
issue — **CASSANDRA-20050**.

## Per-bug reproduction status

| Bug | Decision | State |
| --- | --- | --- |
| CASSANDRA-20050 | Reproduce (hand-crafted, buggy 4.0.14) | ✅ **reproduced in kind** |
| other 99 | Not reproducible via the skill in kind (see findings table) | ⛔ documented, not attempted |

### CASSANDRA-20050 run log
- Problem file already present: `sregym/conductor/problems/auto_cassandra_20050.py`
  (hand-crafted mode: `db_version=4.0.14`, `source_git_ref=cassandra-4.0.14`, explicit cqlsh
  reproducer, `continuous_reproducer=True`).
- Verified base image `k8ssandra/cass-management-api:4.0.14-ubi8` exists and tag `cassandra-4.0.14`
  exists; problem loads in `ProblemRegistry` (144 problems total).
- STAGE 1 (clone + `ant jar` build of 4.0.14 + kind load): ✅ done (~2.5 min).
- STAGE 2 (deploy cert-manager + K8ssandra operator + 3-node cluster): ✅ cluster healthy, but
  `app.deploy()` itself **raised at the 600s readiness timeout** (kind first-boot took ≈660s — see
  ENV-2/BUG-4 in findings). Also had to create an `openebs-hostpath` StorageClass first (ENV-1):
  the CR hardcodes that class, absent on stock kind, leaving PVCs Pending.
- STAGE 3 (run cqlsh reproducer): ✅ done. The deployed image
  `docker.io/k8ssandra/cass-management-api:4.0.14-ubi` is already buggy 4.0.14, so the reproducer was
  run directly against the live cluster (no image swap needed).
- In-cluster verification: ✅ **bug confirmed.** `cqlsh` in pod `sregym-cassandra-dc1-default-sts-0`:
  - DESC clustering order → `InvalidRequest … Invalid user type literal for loc of type frozen<point>`
    (cqlsh exit code 2 = bug present).
  - ASC control (same schema) → INSERT succeeds, row returned (exit 0). Isolates the bug to the
    `DESC`/`ReversedType` path. Full output captured in `repro-findings.md` Part 3.
