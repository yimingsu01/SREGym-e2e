# CASSANDRA-15433 — Pending ranges are not recalculated on keyspace creation

## Issue (primary source: /tmp/jira_repro/CASSANDRA-15433.json)
- Summary: "Pending ranges are not recalculated on keyspace creation"
- Component: Cluster/Membership
- fixVersions: 3.0.26, 3.11.12, **4.0.2**, 4.1-alpha1, 4.1
- Buggy image: **cassandra:4.0.1**  | A/B fixed control: **cassandra:4.0.2** (4.0.2 <= 4.0.20 ceiling)

### Mechanism (from body)
When a node begins bootstrapping, Cassandra recalculates pending tokens for each keyspace that
EXISTS when the BOOT state change is observed (StorageService.handleState*). When a keyspace is
CREATED *after* that, while a node is in BOOT/BOOT_REPLACE, pending ranges are NOT recalculated
(around Schema.merge). Result: writes for the new keyspace are not routed to the joining node, so
after bootstrap completes the joined node is missing data for that keyspace (silent data loss).

### Body's stated reproducer (followed verbatim)
1. Join a node in BOOT mode
2. Create a keyspace
3. Send writes to that keyspace
4. On the joining node, observe `nodetool cfstats` records ZERO writes to the new keyspace

Tag hint (topology=ring, trigger="node in BOOT + CREATE KEYSPACE + writes -> joining node
receives zero writes") matches the body. tag_correction: none.

## Environment
- kind cluster context kind-kind (4 nodes). Namespace: **repro-15433**. Keyspace: **ks15433**.
- Topology: 2 NORMAL nodes (StatefulSet `cass`, replicas=2, cluster_name=repro, ephemeral storage)
  + 1 JOINER as a bare pod `joiner` (NOT in the StatefulSet, because its CQL transport is down
  during bootstrap so the cqlsh readiness probe would never pass).
- Joiner held in BOOT via `JVM_EXTRA_OPTS=-Dcassandra.ring_delay_ms=600000`: it announces BOOT to
  gossip and then sleeps 600s in the pending-range window. We act inside that window.
- Images loaded into all 4 kind nodes by importing `docker save` tars with
  `ctr --namespace=k8s.io images import` (dropping `--all-platforms --digests`, which is what made
  `kind load` fail with "content digest ... not found"). See tooling_findings.

---

## A) BUGGY RUN — cassandra:4.0.1

### Ring healthy (2x UN) before joiner
```
$ kubectl exec -n repro-15433 cass-0 -- nodetool status
UN  10.244.3.101  88.55 KiB  16      100.0%   32c666bb-...  rack1
UN  10.244.2.103  74.11 KiB  16      100.0%   003e2a3a-...  rack1
```

### Joiner reaches BOOT mode (UJ) and sleeps in pending-range window
joiner log:
```
StorageService.java:1619 - JOINING: sleeping 600000 ms for pending range setup
```
ring as seen by NORMAL coordinator cass-0 (joiner = 10.244.1.128 is UJ = Up/Joining = BOOT):
```
UN  10.244.3.101  74.03 KiB  16      100.0%   32c666bb-...  rack1
UN  10.244.2.103  74.11 KiB  16      100.0%   003e2a3a-...  rack1
UJ  10.244.1.128  20.74 KiB  16      ?        490cb509-...  rack1
```

### Step 2+3: CREATE KEYSPACE (RF=2) + 50 writes via NORMAL coordinator cass-0 (joiner still UJ)
```
$ kubectl exec -n repro-15433 cass-0 -- cqlsh -e \
   "CREATE KEYSPACE IF NOT EXISTS ks15433 WITH replication = {'class':'SimpleStrategy','replication_factor':2};"
$ kubectl exec -n repro-15433 cass-0 -- cqlsh -e \
   "CREATE TABLE IF NOT EXISTS ks15433.t (id int PRIMARY KEY, v text);"
# 50 INSERTs id=1..50
$ kubectl exec -n repro-15433 cass-0 -- cqlsh -e "SELECT count(*) AS n FROM ks15433.t;"
 n
----
 50          <-- 50 rows present in the cluster, written at RF=2
```
Joiner still UJ at observation time:
```
UJ  10.244.1.128  110.92 KiB  16      ?        490cb509-...  rack1
```

### Step 4: BUGGY SIGNATURE — `nodetool cfstats` on the JOINER (BOOT node)
The joiner KNOWS the schema (Keyspace ks15433 / Table t appear), is in `Mode: JOINING`, yet has
received ZERO of the 50 RF=2 writes:
```
$ kubectl exec -n repro-15433 joiner -- nodetool cfstats ks15433.t
Keyspace : ks15433
	Read Count: 0
	Write Count: 0
		Table: t
		SSTable count: 0
		Space used (live): 0
		Local read count: 0
		Local write count: 0
```
`nodetool netstats` on joiner: `Mode: JOINING` (confirms BOOT mode at observation).
cqlsh on joiner: ConnectionRefused on 9042 (CQL down during bootstrap — expected; nodetool/JMX is up).

**VERBATIM BUGGY SIGNATURE:** `Local write count: 0`
(on the joiner's ks15433.t, after 50 RF=2 writes through a NORMAL coordinator while the joiner is
in BOOT mode). Equivalently `Write Count: 0` at the keyspace level. This is exactly the body's
predicted symptom: the joining node received zero writes for the keyspace created during BOOT.

---

## B) A/B CONTROL — cassandra:4.0.2 (fixed)  — IDENTICAL workload

Same namespace, same topology rebuilt on 4.0.2: 2 NORMAL nodes (StatefulSet) + joiner bare pod with
`ring_delay_ms=600000`. Joiner reached BOOT:
```
joiner log: StorageService.java:1633 - JOINING: sleeping 600000 ms for pending range setup
ring:  UN 10.244.1.129 ... ; UN 10.244.2.105 ... ; UJ 10.244.3.103 ...   (joiner UJ = BOOT)
```
(Note: the "sleeping ... for pending range setup" log moved from StorageService:1619 in 4.0.1 to
:1633 in 4.0.2 — consistent with the fix inserting code in this bootstrap path.)

Identical CREATE KEYSPACE ks15433 (RF=2) + CREATE TABLE t + 50 INSERTs via NORMAL coordinator
cass-0, joiner still UJ, cluster count = 50.

### CONTROL RESULT — `nodetool cfstats` on the 4.0.2 JOINER (BOOT node)
The joiner DID receive the writes for the ranges it owns as a pending replica:
```
$ kubectl exec -n repro-15433 joiner -- nodetool cfstats ks15433.t
Keyspace : ks15433
	Write Count: 36
		Table: t
		Number of partitions (estimate): 35
		Memtable cell count: 36
		Local write count: 36
		Local write latency: 0.109 ms
```

## CONTRAST (the whole result)
| version | joiner state | workload | joiner `Local write count` on ks15433.t |
|---------|--------------|----------|------------------------------------------|
| **4.0.1 (buggy)**  | UJ / BOOT | 50 RF=2 writes via NORMAL coordinator | **0**  (data loss) |
| **4.0.2 (fixed)**  | UJ / BOOT | identical | **36** (writes routed to pending replica) |

Buggy 4.0.1: the keyspace created during BOOT did not trigger a pending-range recalculation, so
the joining node was excluded from all writes -> 0. Fixed 4.0.2: pending ranges are recalculated
on keyspace creation, so the joining node receives writes for its pending ranges -> 36. Matches the
body's mechanism and predicted symptom exactly.

## Tooling findings
- `kind load docker-image cassandra:4.0.1` (and 4.0.2) FAILED with
  `ctr: content digest sha256:... not found` (and image-archive load failed similarly). Root cause:
  kind's importer runs `ctr ... images import --all-platforms --digests ...`, which requires
  multi-platform manifest digests not present in single-platform `docker save` tars on this host.
  Workaround used: `docker save cassandra:<v> -o t.tar` then
  `docker exec -i <node> ctr --namespace=k8s.io images import --snapshotter=overlayfs - < t.tar`
  on each of the 4 nodes (WITHOUT --all-platforms/--digests). This tagged the images correctly
  (`docker.io/library/cassandra:4.0.1` / `:4.0.2`) on all nodes. Recorded only; not fixed.

## Teardown
Namespace repro-15433 deleted with `kubectl delete ns repro-15433 --wait=false` after writing this log.


