# CASSANDRA-20449 — Reproduction Evidence Log

## Bug
**Summary:** Serialization can lose complex deletions in a mutation with multiple collections in a row
**Buggy version:** cassandra:5.0.3
**Fixed in:** 5.0.4, 6.0-alpha1, 6.0  (A/B control image: cassandra:5.0.4, 4 <= ceiling 8)
**Components:** Legacy/Local Write-Read Paths, Local/Commit Log
**Disposition:** REPRODUCED (verbatim wrong result row + physical SSTable divergence + clean 5.0.4 control)

## Mechanism (from Jira body — ground truth)
A `Mutation` carrying multiple collection columns where one column is a *replacement*
(`SET s2 = {2}`, which generates a complex deletion / collection-level tombstone) and others are
*appends* (`s1 = s1 + {2}`, `s3 = s3 + {2}`). During inter-node Mutation serialization the complex
deletion accompanying the replacement is LOST. The coordinator applies the mutation correctly from the
in-memory object; the peer that receives the *serialized* copy treats the s2 replacement as a mere
append/update, so the old element {1} survives and the replica shows the merged set {1, 2}.
`read_repair='NONE'` keeps the divergence from being healed. Body's reproducer is an in-JVM 2-node dtest
(`testMultipleSetsComplexDeletion`); reproduced here on a real 2-node kind ring with plain CQL.

Classifier hint (topology=ring, confidence=H) — CONFIRMED correct.

## Topology
2-node Cassandra StatefulSet ring in kind ns `repro-20449` (buggy 5.0.3) and `repro-20449-ctrl`
(control 5.0.4). Keyspace `ks20449` RF=2 (NetworkTopologyStrategy dc1:2) — both nodes are replicas.
Ephemeral storage. Both rings showed 2x UN.

## Workload (identical on buggy + control)
```
CREATE KEYSPACE ks20449 WITH replication = {'class':'NetworkTopologyStrategy','dc1':2};
CREATE TABLE ks20449.multi_collection (k int, c int, s1 set<int>, s2 set<int>, s3 set<int>,
    PRIMARY KEY (k, c)) WITH read_repair = 'NONE';
-- via cqlsh on cass-0 (coordinator), CONSISTENCY ALL:
CONSISTENCY ALL;
INSERT INTO ks20449.multi_collection (k, c, s1, s2, s3) VALUES (0, 0, {1}, {1}, {1});
UPDATE ks20449.multi_collection SET s2 = {2}, s1 = s1 + {2}, s3 = s3 + {2} WHERE k = 0 AND c = 0;
```
Read step: connect to EACH pod's own cqlsh and `CONSISTENCY ONE; SELECT ...` (mimics dtest
`executeInternal` — a pure per-replica read with no coordinator reconciliation, which would otherwise
heal the divergence via the higher-timestamp tombstone).

=====================================================================
## BUGGY 5.0.3 — RESULT

### CL ONE per-replica reads (one node correct, one diverged)
```
########## NODE A (s2 correct = {2}) ##########
 k | c | s1     | s2  | s3
---+---+--------+-----+--------
 0 | 0 | {1, 2} | {2} | {1, 2}

########## NODE B (s2 WRONG = {1, 2}) ##########  <-- BUG
 k | c | s1     | s2     | s3
---+---+--------+--------+--------
 0 | 0 | {1, 2} | {1, 2} | {1, 2}
```
The ONLY differing cell across replicas is **s2** (the set *replacement*). s1 and s3 (the appends) are
identical {1, 2} on both nodes. One replica lost the s2 complex deletion and merged {1} with {2}.

### Physical proof — sstabledump after `nodetool flush ks20449 multi_collection` on each node

cass-0 (CORRECT) s2 cells:
```
{ "name" : "s2", "deletion_info" : { "marked_deleted" : "2026-06-12T07:56:58.402797Z", "local_delete_time" : "2026-06-12T07:56:58Z" } },
{ "name" : "s2", "path" : [ "2" ], "value" : "", "tstamp" : "2026-06-12T07:56:58.402798Z" }
```
-> s2 complex deletion timestamp = .402797Z (= the UPDATE's timestamp); only element [2] survives. Correct.

cass-1 (BUGGY) s2 cells:
```
{ "name" : "s2", "deletion_info" : { "marked_deleted" : "2026-06-12T07:56:58.389227Z", "local_delete_time" : "2026-06-12T07:56:58Z" } },
{ "name" : "s2", "path" : [ "1" ], "value" : "" },
{ "name" : "s2", "path" : [ "2" ], "value" : "", "tstamp" : "2026-06-12T07:56:58.402798Z" }
```
-> s2 complex deletion timestamp = .389227Z (= the INSERT's base timestamp, NOT the UPDATE's .402797Z).
Element [1] (old value) SURVIVED alongside [2] => set = {1, 2}. The complex deletion that accompanied
the s2 replacement was dropped during serialization to this peer; it kept the older INSERT-time
collection tombstone. This is the exact bug.

(For comparison, s1/s3 on BOTH nodes carry deletion ts .389227Z + both elements [1],[2] — those are
appends, correctly preserved, so {1,2} is the right answer for them. Only s2 should have lost element 1.)

=====================================================================
## CONTROL 5.0.4 — RESULT (fix confirmed)

### CL ONE per-replica reads — NO divergence
```
########## CONTROL cass-0 CL ONE read ##########
 0 | 0 | {1, 2} | {2} | {1, 2}
########## CONTROL cass-1 CL ONE read ##########
 0 | 0 | {1, 2} | {2} | {1, 2}
```
Both replicas show **s2={2}**.

### Physical proof — cass-1 sstabledump s2 cells (the previously-buggy peer)
```
{ "name" : "s2", "deletion_info" : { "marked_deleted" : "2026-06-12T07:58:05.051552Z", "local_delete_time" : "2026-06-12T07:58:05Z" } },
{ "name" : "s2", "path" : [ "2" ], "value" : "", "tstamp" : "2026-06-12T07:58:05.051553Z" }
```
-> s2 complex deletion timestamp = .051552Z (= the UPDATE's timestamp); only element [2] present.
The complex deletion for the s2 replacement was correctly serialized to the peer. Element [1] is gone.
=> s2 = {2} on the peer. Fixed.

## A/B contrast (the proof)
| Node-that-received-serialized-mutation | s2 elements on disk | s2 complex-deletion ts | s2 value |
|---|---|---|---|
| 5.0.3 buggy peer | [1] and [2] | .389227Z (INSERT ts — STALE) | {1, 2}  WRONG |
| 5.0.4 fixed peer | [2] only      | .051552Z (UPDATE ts — correct) | {2}     CORRECT |

Identical workload, identical topology, single binary version difference -> the divergence appears on
5.0.3 and vanishes on 5.0.4. Reproduced.

## Commands used (key ones)
- kubectl apply -f /tmp/cass-20449-buggy.yaml ; /tmp/cass-20449-ctrl.yaml
- kubectl rollout status statefulset/cass -n repro-20449 --timeout=900s
- kubectl exec -n <ns> cass-0 -- cqlsh -f <schema/workload>.cql   (CONSISTENCY ALL)
- kubectl exec -n <ns> cass-{0,1} -- cqlsh -f read-one.cql        (CONSISTENCY ONE)
- kubectl exec -n <ns> cass-{0,1} -- nodetool flush ks20449 multi_collection
- kubectl exec -n <ns> cass-{0,1} -- /opt/cassandra/tools/bin/sstabledump <Data.db>

## Tooling notes
- The pod template's CASSANDRA_SEEDS/snitch/RF=2 ring worked out of the box.
- `USING CONSISTENCY` is NOT valid CQL3 inline syntax — consistency is a cqlsh session command
  (`CONSISTENCY ALL;` / `CONSISTENCY ONE;`) before the statement. Used that.
- `sstabledump` is not on PATH in the cassandra:5.0.x image; it lives at
  /opt/cassandra/tools/bin/sstabledump.
- The CL ONE cqlsh-from-pod read is routed by the snitch to whichever replica is "closest", so the
  read showing s2={2} vs s2={1,2} did not deterministically map to cass-0 vs cass-1; the
  sstabledump on each node's local Data.db is the authoritative per-replica evidence and removes any
  read-routing ambiguity.

## Teardown
kubectl delete ns repro-20449 --wait=false
kubectl delete ns repro-20449-ctrl --wait=false
