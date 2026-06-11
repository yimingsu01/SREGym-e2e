# Reproduction progress — bugs.txt (Apache Cassandra DB-behavior bugs)

## Scope (current)
`bugs.txt` was re-triaged from the original 100 cached Jira issues and rewritten to contain **only
DB-behavior bugs** (CI/test-logic and internal-tooling dropped). **51 bugs** remain, grouped:
cql-semantics 13, storage-engine 9, distributed-multinode 23, other-db-behavior 6.

Reproduction uses the **stock-image fast path**: for a bug fixed in a released `X.Y.Z`, the buggy
version is `patch − 1` and the official `cassandra:<buggy>` image already contains the bug, so a
single stock pod reproduces it (no source build). Where a fixed image exists, the same workload is run
on the fixed version as an A/B control. Helper: `repro_helper.sh` (session files dir).

- Deployable (released image): **19 / 51**
- Trunk-only (no released image, fixed only in 6.0-alpha/6.0/7.x): **32 / 51**

## Outcome summary
| Disposition | Count | Bugs |
| --- | --- | --- |
| ✅ reproduced (in kind) | **6** | 20050, 21348, 21065, 20972, 21057, 21092 |
| not-reproducible (path shadowed) | 2 | 20915, 20982 |
| not-observable (internal refinement/hardening) | 2 | 20917, 21389 |
| blocked-hard (mTLS / multi-node / timing / no reproducer) | 8 | 21219, 20871, 21332, 20877, 21132, 21428, 20976, 21290 |
| blocked-risk (shared-disk) | 1 | 21245 |
| blocked-no-image (trunk-only) | 32 | all `deployable=0` |

## Reproduced bugs (all with controls — see `repro-findings.md` Part 3)
| Bug | Buggy → Fixed | Category | One-line trigger |
| --- | --- | --- | --- |
| CASSANDRA-20050 | 4.0.14 → 4.0.15 | cql-semantics | `frozen<UDT>` clustering key + `CLUSTERING ORDER BY DESC` rejects a valid INSERT |
| CASSANDRA-21348 | 5.0.8 (+config) | cql-semantics | `check_data_resurrection` on → `SELECT system_views.settings` `ClassCastException` |
| CASSANDRA-21065 | 5.0.6 → 5.0.7 | storage-engine | `nodetool garbagecollect` (UCS + `only_purge_repaired_tombstones`, ≥2 unrepaired sstables) → CME |
| CASSANDRA-20972 | 5.0.5 → 5.0.6 | storage-engine | range tombstone + higher-ts row + `SELECT DISTINCT … token(id)>MIN` → `IllegalStateException` |
| CASSANDRA-21057 | 4.1.10 → 4.1.11 | other-db-behavior | trip disk-usage guardrail FULL, disable threshold → gossip `DISK_USAGE` stuck `FULL` |
| CASSANDRA-21092 | 5.0.6 → 5.0.7 | distributed-multinode | `sstableloader` 3.11 sstables w/ zero-copy → `AssertionError: Filter should not be serialized in old format` |

## Method notes
- A bug's **buggy version is the released fix patch − 1** (e.g. fix 5.0.6 ⇒ buggy 5.0.5). Running on
  the fix version is itself the control (proven on 20972: 5.0.5 fails, 5.0.6 clean).
- Single-node pod suffices for cql-semantics and most storage bugs (`nodetool flush/garbagecollect`).
  `disableautocompaction` is needed to retain ≥2 sstables for compaction-iteration bugs (21065).
- Config-gated bugs (21348) are reproduced by appending an active block to `cassandra.yaml` in the pod
  `command` before `docker-entrypoint.sh cassandra -f`.
- Cross-version bugs (21092) are reproduced by generating sstables on a 3.11.19 pod and `sstableloader`-ing
  them into a 5.0.x pod.

## What is NOT reproduced, and why (see `repro-findings.md` Part 4 for the full table)
- **2 internal-path bugs (20915, 20982):** the buggy code is shadowed by earlier client validation /
  a disabled feature, so cqlsh cannot reach it.
- **2 internal refinements (20917, 21389):** no client-visible behavior change to assert.
- **8 blocked-hard:** need full mTLS cert setup (21219), repaired-data + counter preconditions (20871),
  in-JVM multi-node dtests (21332/21132), multi-node + topology/partition timing (20877/21428), or have
  no concrete reproducer (20976/21290).
- **1 blocked-risk (21245):** needs data-dir free space < the table's uncompressed size; the kind node
  fs is shared by all repro pods, so filling it would risk crashing co-located pods.
- **32 trunk-only:** fixed only in 6.0-alpha/6.0/7.x → no released image → would need a custom trunk
  build (and a base image that likely does not exist).

## Cluster state
Single-node stock pods (namespaces): `cass-3-11-19`, `cass-4-0-18/19/20`, `cass-4-1-10/11`,
`cass-5-0-5/6`, `repro-smoke`(5.0.8), `cass-21348`(5.0.8+config). The original 20050 3-node K8ssandra
cluster remains in `k8ssandra-operator`. Per-bug keyspaces keep version-pods shareable across bugs.

---

## Follow-up (Phase 3): attempting the 9 previously-blocked bugs — IN PROGRESS
New directive: **skip** the not-reproducible (20915, 20982), not-observable (20917, 21389), and
trunk-only (32) bugs; **attempt** the 8 blocked-hard + 1 blocked-risk. A 4-node kind cluster is
available (`kind-control-plane` + `kind-worker{1,2,3}`), enabling real multi-node Cassandra rings.

Confirmed image ceilings still hold (Docker Hub has **no** 5.0.9 / 4.0.21 / 4.1.12). So fixed-image
controls exist for 21219→5.0.7, 20871→4.0.20, 21132→5.0.7, 20877→4.0.20; 21245 (fix 5.0.9) and
21428 (fix 4.0.21/4.1.12/5.0.9) have no fixed image → use within-version evidence.

Exact reproducers extracted from cached Jira bodies (`/tmp/jira_issues/*.json`):

| Bug | Buggy | Plan | Status |
| --- | --- | --- | --- |
| 21245 | 5.0.8 | DeflateCompressor table, big uncompressed/tiny compressed; shrink available space via `max_space_usable_for_compactions_in_percentage` (no real disk fill); `nodetool compact` → "Not enough space" with `expected write size`=uncompressed | **ATTEMPTING** — pod `cass-21245` up, 2 sstables (53K each on disk, ~20MB uncompressed), first `nodetool compact` did **not** trip error; need to verify `getExpectedCompactedFileSize` path / lever math |
| 21219 | 5.0.6 | mTLS `MutualTlsAuthenticator` + client cert; regular user `ADD IDENTITY` binding cert to superuser (CVE-2026-27314) | pending |
| 20871 | 4.0.19 | `repaired_data_tracking_for_range_reads_enabled=true` + counter cells in **repaired** sstables (offline `sstablerepairedset`) → read AIOOBE in `CounterContext.headerLength` | pending |
| 21132 | 5.0.6 | 3-node ring, many keyspaces/tables + SAI indexes → gossip index-status encoding `AssertionError` at startup | pending (multi-node) |
| 20877 | 4.0.19 | ≥2-node ring, incremental repair, bootstrap a node (range movement) → FINALIZED `system.repairs` rows never cleaned (`isSuperseded`=false) | pending (multi-node) |
| 21428 | 4.0.20 | multi-node, ECHO_REQ timeout via transient partition → stale `inflightEcho` entry, node stuck DOWN | pending (multi-node + partition) |
| 21332 | 5.0.8 | per-node divergent placement (node2 range-tombstone, node3 stale row), static SAI col, read_repair=NONE → resurrection during Replica Filtering Protection | likely in-JVM-dtest-only; will confirm |
| 21290 | 4.1.11 | empty heartbeat file if crash between create and write | likely non-deterministic; will confirm |
| 20976 | 5.0.5 | BTI sstable AssertionError on token-range query; Jira = mailing-list link only | no concrete reproducer; will confirm |

### 21245 detail (current attempt)
- Pod `cass-21245` = stock `cassandra:5.0.8` with `max_space_usable_for_compactions_in_percentage: 0.0002`
  appended to `cassandra.yaml` (shrinks compaction-available space to ~6.5MB without filling the shared disk).
- Root cause (Jira): `CompactionTask.buildCompactionCandidatesForAvailableDiskSpace` compares
  `cfs.getExpectedCompactedFileSize(...)` against `Directories.getAvailableSpaceForCompactions` =
  `(usableSpace − min_free_space_per_drive) × max_space_usable_for_compactions_in_percentage`. For a
  compressed table the expected size is reported **uncompressed**, so compaction is wrongly denied.
- Built `k21245.bug` (DeflateCompressor, STCS autocompaction off): 2 flushed sstables, **53K each on
  disk**, **~10MB uncompressed each** (`'z'*1MiB × 10` rows per sstable).
- **First `nodetool compact k21245 bug` returned rc=0 with no "Not enough space" log** → the available
  lever didn't trip as predicted. NEXT: re-read 5.0.8 `getExpectedCompactedFileSize` to confirm whether
  it returns uncompressed pre-fix, recompute the lever (may need lower pct or `min_free_space_per_drive`
  bump), and re-trigger; capture `expected write size` to prove it equals the **uncompressed** total.
