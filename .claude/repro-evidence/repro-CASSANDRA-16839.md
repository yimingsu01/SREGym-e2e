# CASSANDRA-16839 — Truncation snapshots unnecessarily created on node startup

- **Buggy version:** cassandra:4.0.1 (ReleaseVersion: 4.0.1)
- **Fixed control:** cassandra:4.0.2 (ReleaseVersion: 4.0.2) — fixVersions list 4.0.2; 2 <= ceiling 20, so A/B control is valid
- **Disposition:** reproduced
- **Topology:** 1 node (single pod). Hint topology=1node and hint trigger both match the Jira body. tag_correction: none.
- **Namespace:** repro-16839 (created by me)
- **Cluster:** existing kind-kind (context kind-kind)

## Primary source (extracted reproducer)
From /tmp/jira_repro/CASSANDRA-16839.json:
"When testing cassandra 4.0 on ccm I noticed that everytime I restart a node, truncation snapshots are
created for the tables system.table_estimates and system.size_estimates."
Reproducer in the body:
```
$ ccm create -n 1 test -s
$ ccm node1 stop ; ccm node1 start ; ccm node1 stop ; ccm node1 start
$ ccm node1 nodetool listsnapshots
... rows like: truncated-1628599001857-table_estimates system table_estimates 0 bytes 13 bytes
```
components: Tool/nodetool. fixVersions: 3.0.26, 3.11.12, 4.0.2, 4.1-alpha1, 4.1.

Translation to kind: single Cassandra pod with an emptyDir mounted at /var/lib/cassandra (so the data dir
and any snapshots survive an in-place container restart). Restart = `kubectl exec ... -- kill 1` (PID 1 =
`cassandra -f`; SIGTERM -> graceful shutdown -> kubelet restarts the container in-place; emptyDir persists).
Then `nodetool listsnapshots`. No workload needed — the bug fires on the empty system estimates tables
(matches the 0-byte snapshots in the report).

## Deployment
Two pods in repro-16839, each with emptyDir at /var/lib/cassandra, pinned to nodes that already had the
images cached locally (Docker Hub was returning HTTP 429 unauthenticated-pull-rate-limit, so
imagePullPolicy: IfNotPresent + nodeName pinning was required):
- cass-buggy  -> cassandra:4.0.1, nodeName kind-worker2 (4.0.1 cached there)
- cass-fixed  -> cassandra:4.0.2, nodeName kind-worker3 (4.0.2 cached there)

Pod env: MAX_HEAP_SIZE=1024M, HEAP_NEWSIZE=256M, CASSANDRA_DC=dc1,
CASSANDRA_ENDPOINT_SNITCH=GossipingPropertyFileSnitch. Both reached Ready and CQL answered
`SELECT now() FROM system.local`.

## Evidence

### BASELINE — after the FIRST boot (no restarts yet)
```
$ kubectl exec -n repro-16839 cass-buggy -- nodetool listsnapshots
Snapshot Details:
Snapshot name                           Keyspace name Column family name True size Size on disk
truncated-1781238402833-table_estimates system        table_estimates    0 bytes   13 bytes
truncated-1781238400142-size_estimates  system        size_estimates     0 bytes   13 bytes

Total TrueDiskSpaceUsed: 0 bytes

$ kubectl exec -n repro-16839 cass-fixed -- nodetool listsnapshots
Snapshot Details:
There are no snapshots
```
=> Buggy 4.0.1 ALREADY produced one truncated-* pair on the very first boot. Fixed 4.0.2 produced none.

### Restarts (in-place container restart via `kill 1`)
Restart #1 then Restart #2 performed on BOTH pods. Verified restartCount incremented to 2 on each, CQL
came back each time.
```
cass-buggy restartCount: 0 -> 1 -> 2   image cassandra:4.0.1
cass-fixed restartCount: 0 -> 1 -> 2   image cassandra:4.0.2
```

### AFTER 2 RESTARTS (3 total boots)
```
$ kubectl exec -n repro-16839 cass-buggy -- nodetool listsnapshots
Snapshot Details:
Snapshot name                           Keyspace name Column family name True size Size on disk
truncated-1781238402833-table_estimates system        table_estimates    0 bytes   13 bytes
truncated-1781238400142-size_estimates  system        size_estimates     0 bytes   13 bytes
truncated-1781238601577-table_estimates system        table_estimates    9.84 KiB  9.87 KiB
truncated-1781238523099-table_estimates system        table_estimates    0 bytes   13 bytes
truncated-1781238596313-size_estimates  system        size_estimates     7.4 KiB   7.43 KiB
truncated-1781238522715-size_estimates  system        size_estimates     0 bytes   13 bytes

Total TrueDiskSpaceUsed: 0 bytes

$ kubectl exec -n repro-16839 cass-fixed -- nodetool listsnapshots
Snapshot Details:
There are no snapshots
```

## Analysis / discriminator
The discriminator (per advisor) is monotonic growth of `truncated-<ts>-{size,table}_estimates` rows, one
new pair per boot, each with a DISTINCT timestamp:
- 4.0.1: 1 boot -> 1 pair (2 rows); 3 boots -> 3 pairs (6 rows). Distinct ts: size_estimates =
  1781238400142, 1781238522715, 1781238596313 ; table_estimates = 1781238402833, 1781238523099,
  1781238601577. Exactly one new size_estimates + one new table_estimates snapshot per node start.
- 4.0.2 (fix): zero `truncated-*` snapshots across all 3 boots — "There are no snapshots".

This matches the Jira report verbatim in shape, including the 0-byte (13-byte-on-disk) empty-table
snapshots seen in the ticket. Output is operator-visible via `nodetool listsnapshots` => NOT
"not-observable". Fired on the buggy image and was suppressed by the fixed image => clean reproduction
with A/B control.

## Verbatim buggy signature (literal copy of one output row from cass-buggy 4.0.1)
```
truncated-1781238400142-size_estimates  system        size_estimates     0 bytes   13 bytes
```

## Teardown
`kubectl delete ns repro-16839 --wait=false` (see structured result torn_down).

## Tooling findings
None related to SREGym tooling. Environmental only: Docker Hub returned HTTP 429
(toomanyrequests / unauthenticated pull rate limit) for fresh image pulls. Worked around without editing
any repo/tooling by pinning each pod (nodeName) to the kind worker that already had the required image
cached and setting imagePullPolicy: IfNotPresent. Note for the harness: the single-pod template has no
PVC/emptyDir and no readiness probe gating CQL; for any restart-persistence bug an emptyDir at
/var/lib/cassandra is required, otherwise a container restart wipes the data dir and the bug appears
not-reproducible.
