# etcd Auto-Construction Report

**Date:** 2026-04-23
**Goal:** Auto-generate 20 SREGym problems from closed etcd GitHub issues (crash bugs + wrong query result bugs).

## Summary

**Result: 0 out of 24 candidate issues are reproducible with the current SREGym framework.**

**UPDATE (2026-04-27): 2 crash bugs confirmed reproducible with manual deployment (outside the auto-construction pipeline). See "Confirmed Reproducible Bugs" section below.**

**UPDATE (2026-04-28): 5 total crash bugs confirmed reproducible. 3 new bugs found (#14931 JWT panic, #14110 txn context panic, #14891 nil pointer after SIGSTOP). 10 more issues attempted (5 NOT reproducible). Problem files created for all 5 confirmed bugs.**

**UPDATE (2026-04-28, late): 6 total bugs confirmed (5 crash + 1 behavior). #18089 (watch drops DELETE on compact) reproduced — deterministic logic bug, no failpoints needed. Previous attempt tested wrong version (v3.5.17, already fixed). Corrected to v3.5.15. Also: framework extended with `build_cmd`/`build_image` per-problem overrides and `EtcdDataIntegrityOracle`. Multi-node bugs attempted (3-node StatefulSet on 3-worker kind), 0/4 reproducible — Bitnami chart's static member bootstrapping conflicts with dynamic member operations.**

**UPDATE (2026-04-28, evening): 7 total bugs confirmed (6 crash + 1 behavior). #13937 (auth + snapshot-count restart crash) reproduced on 3-node cluster. Bitnami StatefulSet member rejoin blocker SOLVED: patch `ETCD_INITIAL_CLUSTER_STATE=existing` after `member remove`/`member add`. #20269/#17855 (raft heartbeat race on rejoin) attempted 5x but race too tight on pod network. Problem file created for #13937.**

etcd bugs are fundamentally different from database query bugs. SQL databases have client-reproducible bugs (send a query, get wrong result). etcd bugs involve distributed consensus, crash recovery, WAL replay, and cluster membership — none of which can be triggered with `etcdctl` commands against a running single-node cluster.

## Confirmed Reproducible Bugs (manual reproduction, 2026-04-27)

### #18810 — Defrag crash on disk full (REPRODUCED + fix verified)
- **Buggy:** all v3.5.x through v3.5.16. **Fixed:** v3.5.17 (PR #18842)
- **Trigger:** Deploy with constrained storage (100Mi tmpfs emptyDir), write 200×100KB values to fill disk, run `etcdctl defrag`. Defrag fails with ENOSPC writing `db.tmp`. Any subsequent write crashes with `panic: runtime error: invalid memory address or nil pointer dereference` at `bbolt.(*Tx).Bucket() → batch_tx.go:150`.
- **Root cause:** `defrag()` in `backend.go` nils `batchTx.tx` and `readTx.tx` before copying to temp DB. On ENOSPC failure, transactions stay nil.
- **Fix:** v3.5.17 restores transactions on the failure path (`b.batchTx.tx = b.unsafeBegin(true); b.readTx.tx = b.unsafeBegin(false)`).
- **Fix verification:** v3.5.17 defrag fails with same ENOSPC but etcd stays alive — subsequent writes succeed.
- **Deployment:** `emptyDir: {medium: Memory, sizeLimit: 100Mi}` for data volume. Pod manifest at `/tmp/etcd-defrag-crash.yaml`.

### #14382 — Alarm-only snapshot restart crash (REPRODUCED + fix verified)
- **Buggy:** v3.5.3, v3.5.4. **Fixed:** v3.5.5 (PR #14429)
- **Trigger:** Deploy with `--snapshot-count=5`, run `etcdctl endpoint health` 6 times (each calls `alarm list` through raft apply), kill -9 the etcd process. On restart: `panic: failed to recover v3 backend from snapshot` — permanent CrashLoopBackOff.
- **Root cause:** `endpoint health` internally calls `alarm list`, which goes through raft apply but does NOT advance `consistent_index` in the v3 backend DB. After 5 alarm-only applies, a snapshot is triggered at index 6. After hard kill + restart, etcd looks for `0000000000000006.snap.db` which was never created.
- **Fix:** v3.5.5 ensures `consistent_index` is advanced when executing `alarmList`.
- **Fix verification:** v3.5.5 recovers successfully after identical kill -9 scenario.
- **Deployment:** `shareProcessNamespace: true` for kill -9 from sidecar, `--snapshot-count=5` etcd server flag. Pod manifest at `/tmp/etcd-14382.yaml`.

### #14931 — JWT auth panic on missing claims (REPRODUCED 2026-04-28)
- **Buggy:** v3.5.0 through v3.5.7. **Fixed:** v3.5.8 (PR #15676)
- **Trigger:** Deploy with `--auth-token=jwt,pub-key=...,priv-key=...,sign-method=RS256`. Enable auth. Craft JWT token signed with correct private key but missing `username` and `revision` claims. Send any HTTP/gRPC request with the token.
- **Panic:** `panic: interface conversion: interface {} is nil, not string` at `server/auth/jwt.go:77`
- **Root cause:** `tokenJWT.info()` does `claims["username"].(string)` without nil check. Missing map key returns nil interface{}, type assertion panics.
- **Verified on:** v3.5.4 (built from source)

### #14110 — Serializable readonly txn panic on context cancellation (REPRODUCED 2026-04-28)
- **Buggy:** v3.5.0 through v3.5.4. **Fixed:** v3.5.5 (PR #14178)
- **Trigger:** Populate ~5000 keys with 10KB values. Fire 200 concurrent serializable readonly txn requests (HTTP gateway, 0.3s timeout). SIGSTOP etcd. Wait 2s for contexts to expire. SIGCONT.
- **Panic:** `panic: unexpected error during txn: context canceled` at `server/etcdserver/apply.go:638`
- **Root cause:** `applyTxn()` treats ALL errors from range operations as unexpected and calls `lg.Panic()`. Context cancellation is a normal condition when clients disconnect.
- **Verified on:** v3.5.1, v3.5.4 (built from source)

### #14891 — Nil pointer in warnOfExpensiveReadOnlyTxnRequest (REPRODUCED 2026-04-28)
- **Buggy:** v3.5.5, v3.5.6. **Fixed:** v3.5.7 (PR #14899)
- **Trigger:** Same SIGSTOP/SIGCONT pattern as #14110, but on v3.5.5/v3.5.6 which have the #14110 fix.
- **Crash:** `SIGSEGV: nil pointer dereference` at `server/etcdserver/util.go:143`
- **Root cause:** After #14110 fix, applyTxn returns nil TxnResponse instead of panicking. But `warnOfExpensiveReadOnlyTxnRequest` dereferences `resp.Responses[i].ResponseRange` which is nil.
- **Verified on:** v3.5.5, v3.5.6 (built from source)

### #13937 — Auth + snapshot-count restart crash (REPRODUCED 2026-04-28)
- **Buggy:** v3.5.3 only (regression from PR #13908). **Fixed:** v3.5.4 (PR #13942)
- **Trigger:** Deploy 3-node cluster with `ETCD_SNAPSHOT_COUNT=3`. Enable auth (create root user, grant root role, `auth enable`). Send 20 unauthenticated PUT requests (all fail with "user name is empty"). Force-kill all 3 pods. All pods panic on restart.
- **Panic:** `panic: failed to recover v3 backend from snapshot {"error":"failed to find database snapshot file (snap: snapshot file doesn't exist)"}` at `server/etcdserver/server.go:515`
- **Root cause:** In v3.5.3, auth-rejected requests (empty username) create raft log entries but skip `LockInsideApply()`, so `consistent_index` is never advanced. With `snapshot-count=3`, 3 entries trigger a raft snapshot, but the `.snap.db` file is never written. On restart, etcd panics looking for the missing snapshot file.
- **Verified on:** v3.5.3 (official image `quay.io/coreos/etcd:v3.5.3`), 3-node cluster
- **Deployment:** `--set replicaCount=3 --set-string 'extraEnvVars[0].name=ETCD_SNAPSHOT_COUNT' --set-string 'extraEnvVars[0].value=3'`

### Bugs attempted but NOT reproduced (2026-04-27)
- **#20009** (force-new-cluster): etcd uses member ID from WAL data, not the `--name` flag — no crash with different member's PVC.
- **#20269** (member rejoin race): Raft message timing too tight for Kubernetes DNS-based reproduction. Re-tested after solving StatefulSet rejoin blocker (5 remove/add cycles), race still too tight.

### Bugs attempted but NOT reproduced (2026-04-28, 10 more attempted)
- **#14025** (WAL max entry size): Needs Go client with custom `MaxCallSendMsgSize` to send values >10MB. HTTP gateway limited by gRPC message size. Wrote 10MB value successfully, WAL replay worked.
- **#14733** (revision inconsistency during defrag kill): Kill during defrag doesn't produce visible crash — causes subtle data corruption that's hard to detect, not a panic.
- **#19167** (deadlock during stop while bootstrapping): etcd bootstraps too fast on single node — SIGTERM arrives after bootstrap completes. Needs timing within millisecond bootstrap window.
- **#17146** (nil backend on startup after snapshot kill): Multiple kill-during-write attempts failed to hit the specific moment during snapshot flush. Needs failpoint injection.
- **#13762** (gRPC health check crash): All tested maintenance API edge cases handled gracefully.
- **Various edge cases on v3.5.1**: Range with large limits, watch at revision 0, lease grant with TTL=0, compact to future revision, empty transaction, put with empty key, delete with inverted range — all handled correctly.
- **Concurrent defrag + writes on v3.5.1**: No race condition triggered.
- **Quota alarm + compact + defrag + kill on v3.5.1**: Restarted successfully.
- **Rapid lease grant/revoke cycles on v3.5.4**: No crash after 50 rapid grant/attach/revoke cycles, 20 short-TTL leases, 100 keys on one lease.

## Infrastructure Built

The following infrastructure was built and works correctly end-to-end:

- etcd DB_REGISTRY entry in `db_build_spec.py` (build from source, deploy on kind via Bitnami Helm chart, custom probes, alpine client pods for reproducer execution)
- Bitnami registry/repository split in `generic_db_app.py` for non-Bitnami images (`quay.io/coreos/etcd`)
- etcdctl keyword patterns in `reproducer_extractor.py`
- 24 `auto_etcd_*.py` problem stub files

**Verified working pipeline:** Source clone (v3.5.17) → Go 1.22 build → Docker image → kind load → Helm deploy → image swap (fault injection) → alpine client pod with etcdctl → continuous reproducer with cached-probe readiness check.

## Detailed Issue Analysis

### #18089 — Watch dropping event when compacting on delete (Most Promising)

**Status: NOT REPRODUCIBLE without failpoints**

This was the most promising candidate. The bug is in `server/mvcc/kvstore.go:restoreIntoIndex()` — during startup, etcd's index restoration silently skips tombstone-only keys. This causes a watch to miss DELETE events.

**Critical finding:** The bug ONLY manifests when etcd crashes at a specific moment — after physical compaction completes but BEFORE writing the `finishedCompactRev` marker. The fix PR's test uses failpoint `compactBeforeSetFinishedCompact=panic` to create this state. A normal pod restart writes the marker correctly, so the index restoration works fine.

**Testing performed:**
1. Deployed v3.5.17 buggy build from source ✓
2. Created key, deleted (tombstone at rev N), compacted at N with `--physical` ✓
3. Verified old revisions physically removed ✓
4. Killed etcd pod, waited for restart ✓
5. Watch from tombstone revision after restart → **DELETE event still visible** (bug not triggered)
6. Confirmed `finishedCompactRev` correctly reflects physical state after normal restart

**To reproduce this bug, you would need:** etcd built with failpoint support (`make build FAILPOINT=true`) + HTTP API call to trigger `compactBeforeSetFinishedCompact=panic` at the exact right moment.

### #14294 — etcdctl lease id json output bug

**Status: COSMETIC — not suitable for SREGym**

JSON number precision issue (64-bit lease IDs rounded to 53-bit floats in JSON output). Not a server behavior bug, can't be fixed by SRE actions.

### Crash Bugs — All Need Multi-Node or Process Manipulation (12 issues)

| Issue | Title | Blocker |
|-------|-------|---------|
| #20009 | nil pointer on force-new-cluster | Needs data dir + restart with `--force-new-cluster` flag |
| #20269 | panic on member rejoin after reset | Multi-node + `etcdutl member remove/add` |
| #20340 | panic on 'failed to nodeToMember' | Corrupted member data + restart |
| #20716 | Campaign watch cancel blocks stream | Go `concurrency` package client |
| #19700 | panic comparing uncomparable type | Specific gRPC interceptor chain |
| #19509 | panic: Write after Handler finished | v3.6+ only, Go client with specific request pattern |
| #18853 | Close of already closed channel | Race condition under concurrent requests |
| #18810 | Crash during defrag when out of space | Needs disk space exhaustion during defrag |
| #17855 | Panic on new node joining cluster | Multi-node cluster join |
| #17780 | Revision decreasing after panic | Requires panic during compaction + recovery |
| #17081 | Wrong raft messages cause panic | Multi-node raft message manipulation |
| #17001 | v3client Endpoints() nil pointer | Go `v3client` package direct call |

### Wrong-Result / Behavior Bugs — All Need Special Setup (7 issues)

| Issue | Title | Blocker |
|-------|-------|---------|
| #16666 | Stale linearizable read | `--experimental-wait-cluster-ready-timeout` flag + timing |
| #16002 | Incorrect config brings down cluster | Multi-node with misconfigured member |
| #15243 | MemberList broken after adding member | Multi-node member add |
| #14616 | Leases not revoked with JWT auth | JWT authentication setup |
| #14370 | Durability broken in single node | Process kill + fsync verification |
| #14631 | NewSession hangs after SIGSTOP | Go client + SIGSTOP signal |
| #19261 | lease keep-alive returns success on failure | Specific failure injection |

### Remaining Issues (3)

| Issue | Title | Blocker |
|-------|-------|---------|
| #18777 | raftexample node delete/add | Uses `raftexample` binary, not etcd server |
| #13762 | gRPC health check crashes etcd | Specific gRPC health check API sequence |
| #9006 | Key not deleted when lease expires | Very old issue (master branch era), likely already fixed |

## Why etcd Is Hard for Auto-Construction

1. **No query language:** SQL databases have client-side reproducers (send a query, check result). etcd operations are simple key-value CRUD — bugs hide in consensus, replication, and crash recovery.

2. **Crash recovery bugs dominate:** 15 of 24 issues involve panics or crashes that require process restart, specific failure timing, or failpoint injection.

3. **Multi-node requirement:** 8 of 24 issues specifically need multi-node clusters (raft consensus, member add/remove, leader election).

4. **Go client required:** 5 issues need the Go etcd client library (not `etcdctl`) — they involve specific gRPC streams, concurrency primitives, or internal v3client calls.

5. **Distroless images:** Official etcd images (`quay.io/coreos/etcd`) have no shell, making in-container reproducer execution impossible. Requires sidecar client pods.

6. **LLM reproducer extraction fails:** The reproducer extractor is designed for SQL-style reproducers. etcd issues describe Go test cases and cluster operations, which the LLM can't convert to shell scripts.

## Recommendations

1. **Skip etcd** for auto-construction unless the framework is extended to support:
   - Multi-node deployments (StatefulSet with 3+ replicas)
   - Failpoint-enabled builds (`make build FAILPOINT=true`)
   - Pod kill/restart orchestration from reproducer pods
   - Go client-based reproducers (compile and run Go test code)

2. **Focus auto-construction on databases with SQL-like client interfaces:** MySQL, PostgreSQL, TiDB, CockroachDB — where bugs manifest as wrong query results that can be checked with client commands.

3. **If etcd support is needed,** the most tractable approach is building etcd with failpoints and using the failpoint HTTP API to trigger crash scenarios. This would require:
   - Modified build command: `make build FAILPOINT=true`
   - Reproducer pods with `curl` to trigger failpoints via HTTP
   - RBAC for reproducer pods to delete/restart etcd pods

## Files Created/Modified

### New files
- `sregym/conductor/problems/auto_etcd_*.py` — 24 problem stubs (only #18089 has a hand-crafted reproducer)
- `etcd_auto_construction_report.md` — this file

### Modified files
- `sregym/service/db_build_spec.py` — etcd registry entry, reproducer functions, cached-probe workload
- `sregym/service/apps/generic_db_app.py` — Bitnami registry/repository split for non-Bitnami images
- `sregym/service/reproducer_extractor.py` — etcdctl keyword patterns
