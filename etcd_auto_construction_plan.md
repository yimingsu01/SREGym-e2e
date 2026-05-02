# etcd Auto-Construction Plan

Continuation plan for auto-generating SREGym problems from etcd GitHub issues.

## Current State (2026-04-23)

### Cluster
- kind cluster is running with etcd deployed in namespace `etcd`
- Buggy image `sregym/etcd-patched:3.5.17-24e9297e` is active on pod `sregym-etcd-0`
- OLD continuous reproducer deployment is running (stale — uses timed-out probe, needs redeployment)
- Clean up command: `kubectl delete deployment sregym-etcd-reproducer configmap/sregym-etcd-reproducer -n etcd`

### Files Modified (uncommitted)
- `sregym/service/db_build_spec.py` — etcd registry entry + reproducer functions rewritten
- `sregym/service/apps/generic_db_app.py` — Bitnami registry/repository split
- `sregym/service/reproducer_extractor.py` — etcdctl keyword added
- `sregym/conductor/problems/auto_etcd_18089.py` — hand-crafted problem (the only viable one)
- `sregym/conductor/problems/auto_etcd_*.py` — 23 other stub files (issue_url mode, most will fail)

---

## Task 1: Complete #18089 Lifecycle Validation

Issue: https://github.com/etcd-io/etcd/issues/18089
Bug: Watch drops DELETE event when compacting at the delete tombstone's revision.
Buggy: v3.5.17. Fixed: v3.5.18 (PR #19249).

### Steps Remaining

**Step 3 (redo): Re-deploy continuous reproducer with fixed probe**
The `_etcd_reproducer_workload` in `db_build_spec.py` was just rewritten to cache exit codes in `/tmp/probe_rc` instead of re-running the 3s reproducer inside a 1s probe timeout. Need to:
1. Delete old reproducer: `kubectl delete deployment/sregym-etcd-reproducer configmap/sregym-etcd-reproducer -n etcd`
2. Re-deploy using the problem class (or manually apply the manifest from the workload function)
3. Verify reproducer pod becomes NotReady (bug present → grep for DELETE fails → exit 1 → probe fails)

**Step 3 verification: Run mitigation oracle — should FAIL**
- `ReproducerPodMitigationOracle` with `expect_unready=False` (since `expected_output=None`)
- NotReady = bug present = oracle returns FAIL
- Can test via: `python run-oracle.py --problem auto_etcd_18089` or instantiate directly

**Step 4: Fix the bug — swap to v3.5.18**
- Build v3.5.18 from source: the problem class uses v3.5.17 (buggy). To test the fix, build stock v3.5.18:
  ```python
  # Option A: Build from v3.5.18 tag
  # Need to either create a second image or patch the statefulset to use quay.io/coreos/etcd:v3.5.18
  ```
- Simplest approach: patch the StatefulSet image directly to `quay.io/coreos/etcd:v3.5.18`
  ```bash
  kubectl set image statefulset/sregym-etcd etcd=quay.io/coreos/etcd:v3.5.18 -n etcd
  kubectl rollout status statefulset/sregym-etcd -n etcd --timeout=120s
  ```
- Note: `quay.io/coreos/etcd:v3.5.18` is distroless — same as v3.5.17, works with the Bitnami chart custom probes already configured

**Step 5: Run mitigation oracle — should PASS**
- After swapping to v3.5.18, the reproducer's `grep -q DELETE` should succeed → exit 0 → probe passes → Ready
- Oracle with `expect_unready=False`: Ready = fixed = PASS

### Probe Logic Summary
```
auto_etcd_18089.py:
  expected_output = None (not set)
  continuous_reproducer = True

GenericCustomBuildProblem.__init__:
  expect_unready = (self.expected_output is not None) = False

Reproducer script exit code:
  Bug present → DELETE missing → grep -q DELETE fails → exit 1
  Bug fixed   → DELETE present → grep -q DELETE succeeds → exit 0

Cached probe (/tmp/probe_rc):
  "1" → exit 1 → NotReady → oracle(expect_unready=False) → FAIL (bug present)
  "0" → exit 0 → Ready    → oracle(expect_unready=False) → PASS (fixed)
```

---

## Task 2: Document All 24 Candidates

After validating #18089, write a final report. Here's the status of all candidates:

### Viable (1)
| Issue | Title | Status |
|-------|-------|--------|
| #18089 | Watch dropping event on compacting delete | Hand-crafted reproducer, steps 1-3 done, steps 4-5 pending |

### Had LLM-extracted reproducers but need manual work (4)
| Issue | Title | Blocker |
|-------|-------|---------|
| #19509 | panic: Write called after Handler finished | v3.6 only, needs Go client code |
| #17001 | v3client Endpoints() nil pointer panic | Needs Go client with direct v3client call |
| #20340 | Panic on 'failed to nodeToMember' | Needs corrupted data dir or specific cluster state |
| #14294 | etcdctl lease id json output bug | Minor output format bug, may be reproducible with etcdctl |

### Crash bugs — not reproducible with single-node etcdctl (12)
| Issue | Title | Blocker |
|-------|-------|---------|
| #20009 | nil pointer on re-use storage with force-new-cluster | Needs data dir manipulation + restart |
| #20269 | panic on member rejoin after reset | Multi-node + member reset |
| #20716 | Campaign watch cancel blocks adapter stream | Go client + concurrency package |
| #19700 | panic comparing uncomparable type | Needs specific gRPC interceptor |
| #18853 | Close of already closed channel panic | Race condition, needs concurrent requests |
| #18810 | Crash during defrag when out of space | Needs disk space manipulation |
| #17855 | Panic on new node joining cluster | Multi-node |
| #17780 | Revision decreasing after panic during compaction | Needs panic + recovery cycle |
| #17081 | Wrong raft messages cause panic | Multi-node raft manipulation |
| #16002 | Incorrect config brings down cluster | Multi-node |
| #15243 | MemberList broken after adding member | Multi-node |
| #13762 | gRPC health check crashes etcd | Needs specific gRPC health check sequence |

### Wrong-result / behavior bugs — not reproducible with single-node etcdctl (7)
| Issue | Title | Blocker |
|-------|-------|---------|
| #16666 | stale linearizable read with wait-cluster-ready | Needs specific startup flag + timing |
| #14616 | Leases not revoked with JWT auth | Needs JWT auth setup |
| #14370 | Durability guarantee broken in single node | Needs process kill + fsync verification |
| #14631 | NewSession hangs after SIGSTOP | Needs Go client + SIGSTOP |
| #18777 | raftexample node delete/add abnormality | raftexample binary, not etcd server |
| #19261 | lease keep-alive returns success on failure | Needs specific failure injection |
| #9006 | Key not deleted when lease expires | Very old (master branch), may be fixed |

---

## Task 3: Write Final Report

After completing Task 1, write results to:
1. Memory file (already created, update with final status)
2. Markdown report file in repo root

---

## Key Technical Notes for Next Session

### etcd on Bitnami Helm chart (quay.io/coreos/etcd images)
- `quay.io/coreos/etcd:v*` images are **distroless** — no shell, no sleep, no package manager
- Can't `kubectl exec` into etcd pods for reproducer execution
- Use separate `alpine:3.20` client pod that downloads etcdctl from GitHub releases
- Bitnami chart requires `global.security.allowInsecureImages=true` for non-Bitnami images
- Default Bitnami probes (`/opt/bitnami/scripts/etcd/healthcheck.sh`) don't exist in coreos images — must disable and set custom probes
- Image flags need registry/repository split: `quay.io` → `image.registry=quay.io`, `coreos/etcd` → `image.repository=coreos/etcd`

### Build pipeline
- etcd go.mod specifies Go 1.22 — build system auto-detects and overrides `golang:1.24` → `golang:1.22-bullseye`
- Build command: `make build` → produces `bin/etcd`
- Artifact destination in coreos image: `/usr/local/bin/etcd`

### kind cluster
- StorageClass: `standard` (not `openebs-hostpath`)
- Images need to be loaded: build system does `kind load docker-image` automatically
