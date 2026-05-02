# etcd Bug Reproduction: Challenges & Proposed Fixes

**Date:** 2026-04-28
**Context:** During auto-construction of SREGym problems from 24 etcd GitHub issues, 5 crash bugs were confirmed reproducible on a single-node kind cluster. The remaining 19 could not be reproduced. This document catalogues the challenges encountered and reviewed fixes.

**Bottom line:** 2 challenges are already solved, and 4 are genuine open challenges. The highest-impact fixes are **multi-node etcd deployment** (~8 bugs), **failpoint build support** (~5 bugs), and **data integrity oracle** (~3 bugs).

---

## Solved

### S1. PVC size not enforced on kind

**Challenge:** kind's `rancher.io/local-path` provisioner ignores PVC size limits, so #18810 (defrag ENOSPC) couldn't trigger disk-full conditions.

**Fix (applied):** Use `emptyDir: {medium: Memory, sizeLimit: 100Mi}` via `post_deploy()` patching the StatefulSet. The kernel enforces tmpfs limits at the filesystem level.

**Bugs unblocked:** #18810.

### S2. Distroless etcd images have no shell

**Challenge:** `quay.io/coreos/etcd` has no shell, so reproducer scripts cannot be exec'd into the etcd container.

**Fix (applied):** `_etcd_run_reproducer` in `db_build_spec.py` deploys a separate alpine pod, downloads etcdctl, and runs scripts there. For in-container operations (SIGSTOP, process inspection), use `shareProcessNamespace: true` and run commands from an alpine sidecar targeting PID 1's namespace.

**Bugs unblocked:** all 5 confirmed bugs rely on this.

---

## Open Challenges

### C1. No failpoint injection support

**Challenge:** Bugs like #18089 (watch event drop on compact) require crashing at a precise code point mid-operation. Go failpoints (e.g. `compactBeforeSetFinishedCompact=panic`) are the only way to create the exact corrupt state these bugs require.

**Proposed fix:** Add a `build_cmd` class attribute to `GenericCustomBuildProblem`. Subclasses that need failpoints simply override it:

```python
class AutoEtcd18089(GenericCustomBuildProblem):
    db_name = "etcd"
    build_cmd = "make build FAILPOINT=true"
```

In `GenericCustomBuildProblem.__init__`, if the subclass sets `build_cmd`, patch `spec.build_cmd` before passing it to `GenericDBBuildManager`. The build manager already reads `self.spec.build_cmd` at line 154 of `generic_db_build_manager.py`, so no changes are needed there. The failpoint HTTP API (`POST /failpoints/<name>`) can then be called from reproducer scripts.

**Complexity:** Low. One class attribute + a 3-line check in `__init__`.

**Bugs unblocked:** #18089, #17146, #14733, #14370, and potentially others where crash timing is critical.

### C2. Single-node cluster can't reproduce multi-node consensus bugs

**Challenge:** 8 of 24 candidate bugs require multi-node etcd clusters:
- Raft message manipulation (#17081)
- Member add/remove (#15243, #20269, #17855)
- Leader election and failover (#16002, #16666)
- Split-brain and rejoin (#20009, #13937)

Single-node etcd has no raft peers — consensus, leader election, and membership changes are no-ops.

**Proposed fix:** Deploy a 3-replica etcd StatefulSet (`--set replicaCount=3`) with the Bitnami Helm chart (already supports this). The kind cluster should be created with 4 worker nodes by default to accommodate multi-replica workloads. A 4-worker kind cluster provides the Kubernetes scheduling capacity; `replicaCount=3` in Helm creates the 3-member etcd cluster on top of it.

Add a `multi_node: bool` field to problem classes to select deployment topology.

**Complexity:** High. Requires changes to:
- `_etcd_image_patch` — must patch all 3 container instances
- `_etcd_run_reproducer` — must target specific pods (leader vs follower)
- Fault injection — must support per-pod operations (kill one member, network partition)
- Readiness checks — must wait for all 3 members to join the cluster

**Bugs unblocked:** #17081, #15243, #20269, #17855, #16002, #16666, #20009, #13937.

### C3. Subtle data corruption without crash is undetectable

**Challenge:** #14733 (revision inconsistency during defrag kill) doesn't crash — it silently corrupts data. The framework's crash detection (`_wait_for_any_crash_loop`) can't catch this. Similarly, #18089's symptom is a missing watch event, not a crash.

**Proposed fix:** Add a data integrity oracle that compares expected vs actual state after fault injection. For etcd:
- Verify `etcdctl get` returns expected values for all written keys
- Check revision numbers are monotonically increasing
- Confirm watch streams don't miss events (write N keys, watch should see N events)
- Compare `etcdctl endpoint status` revision across members (multi-node)

**Complexity:** Medium. This extends `MitigationOracle` with etcd-specific verification logic. The oracle needs to know the expected state, which means the fault injection step must record what was written.

**Bugs unblocked:** #14733, #18089 (partial — still needs C1), #14370.

### C4. Go version mismatch for older etcd releases

**Challenge:** The etcd `build_image` in `DB_REGISTRY` is `golang:1.24`, but etcd v3.5.0–v3.5.4 was written for Go 1.16–1.19. Newer Go versions may fail to compile older code due to deprecated APIs or changed module semantics.

**Proposed fix:** Same pattern as C1 — let the subclass override `build_image` as a class attribute. In `__init__`, patch `spec.build_image` before constructing `GenericDBBuildManager`. Subclasses for older etcd versions set `build_image = "golang:1.19"`.

**Verdict:** Partially valid. Go is generally backward-compatible, and `make build` succeeded for v3.5.4 and v3.5.5 during testing with Go 1.24. But this could break for much older versions (v3.4.x) or if future Go releases remove deprecated features.

**Bugs unblocked:** preventive — no specific bug was blocked by this yet.

---

## Deferred

### D1. Go client library requirement

Deferred until more crashing bugs are found that specifically require Go client APIs.

Several bugs need Go-specific APIs unavailable from shell scripts (#14025 needs `MaxCallSendMsgSize`, #20716 needs the `concurrency` package, #14631 needs `concurrency.NewSession`, #17001 needs `v3client.New()`). If this becomes a recurring blocker, the lightest fix is pre-compiling Go reproducer binaries during the build step and including them in the custom image.

### D2. Bootstrap timing window too narrow

Deferred — fundamentally not reproducible on single-node without code instrumentation.

#19167 (deadlock during stop while bootstrapping) needs SIGTERM during a <10ms window. Even a Go binary busy-polling `/proc/<pid>/status` can't reliably hit it. This overlaps with C1 — failpoint support would let you inject a `sleep` into the bootstrap path, widening the window.

---

## Implementation Status (2026-04-28)

| Challenge | Status | Result |
|-----------|--------|--------|
| C1. Failpoint build support | **DONE** | `build_cmd` override in `GenericCustomBuildProblem`. #18089 reproduced (deterministic, no failpoint needed). |
| C2. Multi-node deployment | **DONE** | StatefulSet rejoin blocker SOLVED: patch `ETCD_INITIAL_CLUSTER_STATE=existing` after `member remove`/`member add`. #13937 reproduced (3-node, auth + snapshot-count). #20269/#17855 not reproducible (raft race too tight). |
| C3. Data integrity oracle | **DONE** | `EtcdDataIntegrityOracle` created. #18089 uses `ReproducerPodMitigationOracle` (continuous probe). |
| C4. Go version mapping | **DONE** | `build_image` override in `GenericCustomBuildProblem` (merged with C1). |

**Total reproducible etcd bugs: 7** (6 crash + 1 behavior: #18810, #14382, #14931, #14110, #14891, #18089, #13937)
