# CASSANDRA-20877 — FINALIZED incremental repair sessions not cleaned up after range movement

- **Disposition:** REPRODUCED (verbatim differential signature on the repair-coordinator node)
- **Buggy version:** cassandra:4.0.19  (namespace `repro-20877`)
- **Fixed control:** cassandra:4.0.20  (namespace `ctrl-20877`)
- **Topology:** 3-node ring (RF=2), bootstrapped from an initial 2-node ring to create the range movement
- **Keyspace/table:** `ks20877.t` (SimpleStrategy RF=2), 5 rows
- **Cluster:** kind-kind (4 nodes); pods scheduled on kind-worker / kind-worker2 / kind-worker3

## Bug (from JIRA description = primary source)
`system.repairs` is local per node and pruned by `LocalSessions#cleanup()` every
`cassandra.repair_cleanup_interval_seconds` (default 10m). It deletes FINALIZED sessions older than
`cassandra.repair_delete_timeout_seconds` (default 1d) **only if** `LocalSessions#isSuperseded(session)`
is true — i.e. every range+table the session covered has since been re-repaired by a newer session.
After a node bootstraps/decommissions, a set of ranges moves off the old nodes; those moved ranges are
no longer re-repaired on the old nodes, so the last pre-movement session is never superseded and its
FINALIZED row is kept **forever**. Source confirmed in
`/tmp/sregym-sources/cassandra-cassandra-4.0.14/src/java/org/apache/cassandra/repair/consistent/LocalSessions.java`:
- `AUTO_DELETE_TIMEOUT` (L135) = `cassandra.repair_delete_timeout_seconds`, `CLEANUP_INTERVAL` (L140) = `cassandra.repair_cleanup_interval_seconds` (both JVM-tunable).
- `isSuperseded` (L240): `if (state==null || minRepaired <= session.repairedAt) return false;`
- `cleanup` (L450-457): `else if (shouldDelete(...)) { if (FINALIZED && !isSuperseded) -> "Skipping delete..." }`
- `ConsistentSession.State`: FINALIZED = ordinal **4**.

## Reproducer (exact)
Both rings identical, only the image differs. JVM props on ALL pods (via `JVM_EXTRA_OPTS`) to make the
1-day timeout / 10-min interval testable in budget:
`-Dcassandra.repair_delete_timeout_seconds=30 -Dcassandra.repair_cleanup_interval_seconds=20`
(verified active: `ps aux` on repro/cass-0 shows both flags).

1. Deploy 2-node StatefulSet (RF-capable), wait for 2 UN.
2. `CREATE KEYSPACE ks20877 ... RF=2`; `CREATE TABLE ks20877.t`; INSERT 5 rows; `nodetool flush` both nodes.
3. **S1** = `nodetool repair ks20877` on cass-0 (default = incremental). Both rings: "Repair command #1 finished".
   Confirm S1 FINALIZED (state=4) on both nodes via `SELECT parent_id,state FROM system.repairs`.
4. **Range movement**: `kubectl scale statefulset/cass --replicas=3` -> bootstrap cass-2. Wait for 3 UN.
   Ownership shifted 100%/100% -> ~64.7%/59.3%/76.0% (cass-0 & cass-1 ceded ranges to cass-2).
5. **S2** = flush all 3 nodes + `nodetool repair ks20877` on cass-0 again. "Repair command #2 finished".
   S2 (coordinated on cass-0) advances repairedAt for every range cass-0 still replicates.
6. Wait > delete_timeout + 2*cleanup_interval (~90s; S1 ends up >5 min old). Re-query `system.repairs`.

## Ring / session IDs
| ring | image | S1 parent_id (pre-move) | S2 parent_id (post-move) |
|------|-------|-------------------------|--------------------------|
| repro | 4.0.19 | `ed5be870-65dc-11f1-8c53-6deb776ceda9` | `4f1107d0-65dd-11f1-8c53-6deb776ceda9` |
| ctrl  | 4.0.20 | `eedbf8c0-65dc-11f1-a6d2-ad5fe76209b1` | `588d7780-65dd-11f1-a6d2-ad5fe76209b1` |

## FINAL system.repairs (after S2 + ~90s of cleanup passes; S1 aged >5 min vs 30s timeout)
```
##### repro-20877 (4.0.19 BUGGY) #####           ##### ctrl-20877 (4.0.20 FIX) #####
--- cass-0 ---                                   --- cass-0 ---  (DISCRIMINATOR)
 ed5be870-...  state 4   (S1 SURVIVES)            588d7780-...  state 4   (S2 only; S1 DELETED)
 4f1107d0-...  state 4   (S2)                    (1 rows)
(2 rows)                                         --- cass-1 ---
--- cass-1 ---                                    eedbf8c0-...  state 4   (S1 kept - see note)
 ed5be870-...  state 4   (S1 SURVIVES)            588d7780-...  state 4   (S2)
 4f1107d0-...  state 4   (S2)                    (2 rows)
(2 rows)
```

## VERBATIM BUGGY SIGNATURE  (4.0.19 / cass-0 debug.log — recurs every 20s, forever)
```
DEBUG [OptionalTasks:1] 2026-06-11 21:36:32,411 LocalSessions.java:456 - Skipping delete of FINALIZED LocalSession ed5be870-65dc-11f1-8c53-6deb776ceda9 because it has not been superseded by a more recent session
```
S1 (`ed5be870`) is FINALIZED (state=4), aged far past the 30s delete-timeout, yet skipped every cleanup
pass — because S2 (the newer repair, coordinated on cass-0) cannot supersede the ranges that MOVED to
cass-2, so `isSuperseded` stays false. The row is therefore retained indefinitely.

## CONTROL (A/B) — IDENTICAL workload on 4.0.20 / cass-0 debug.log
```
DEBUG [OptionalTasks:1] 2026-06-11 21:34:25,511 LocalSessions.java:482 - Skipping delete of FINALIZED LocalSession eedbf8c0-... because it has not been superseded by a more recent session
DEBUG [OptionalTasks:1] 2026-06-11 21:34:45,511 LocalSessions.java:487 - Auto deleting repair session LocalSession{sessionID=eedbf8c0-65dc-11f1-a6d2-ad5fe76209b1, state=FINALIZED, coordinator=/10.244.3.12:7000, ..., ranges=[(-6707851540221898144,-6254331955784065410], ... 32 ranges spanning ~the whole original 2-node ring ...], participants=[/10.244.3.12:7000, /10.244.2.14:7000], ...}
```
The fix DELETES S1 (`eedbf8c0`) on cass-0 — even though its range list spans the original 100% 2-node
ring while cass-0 now owns only 64.7% — i.e. the corrected logic ignores ranges no longer owned. Result:
ctrl/cass-0 ends with S2 only (1 row); repro/cass-0 keeps S1+S2 (2 rows) forever. Same workload, opposite
outcome, mechanism named in the log => bug isolated to the version.

## Why cass-0 (not cass-1) is the discriminator
S2 was coordinated ON cass-0, so a default `nodetool repair` advances repairedAt for every range cass-0
still replicates; the ONLY thing that can leave S1 not-superseded on cass-0 is the ranges that moved away.
=> cass-0 is a clean single-variable comparison. cass-1 is NOT the S2 coordinator, so S2 doesn't cover all
of cass-1's still-owned ranges (the ~1/3 replicated by cass-1+cass-2 but not cass-0); thus S1 has genuinely
owned, un-re-repaired ranges on cass-1 and is "not superseded" even under the corrected logic (4.0.20 cass-1
logs `LocalSessions.java:482 ... not been superseded`). cass-1 retaining S1 is EXPECTED and simply does not
discriminate — it is not a control failure. (Retention here is the isSuperseded path, line 456/482 — NOT the
separate `sstables` guard at line 462/487-as-warn; the `!sessionHasData` concern is moot for this result.)

## Tooling findings: none.
## Namespaces created: repro-20877, ctrl-20877 (both torn down after evidence capture).
