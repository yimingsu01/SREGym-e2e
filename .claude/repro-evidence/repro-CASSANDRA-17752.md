# CASSANDRA-17752 — Reproduction Evidence Log

**Summary:** fix restarting of services on gossipping-only members
**Buggy version:** cassandra:4.0.5   |   **Fixed control:** cassandra:4.0.6  (fixVersions include 4.0.6)
**Components:** Legacy/Core, Tool/nodetool
**Disposition:** REPRODUCED (verbatim buggy signature + A/B control)
**Classifier hint:** topology=1node, confidence=M — CONFIRMED correct (tag_correction=none)

## Bug (from Jira body — ground truth)
When a node is started with `-Dcassandra.join_ring=false`, you can still talk to it via CQL
(native/binary transport is up). If you disable it with `nodetool disablebinary`, you CANNOT
re-enable it with `nodetool enablebinary`. Reason: `enablebinary` eventually calls
`StorageService#checkServiceAllowedToStart`, which throws unless the node is in NORMAL state.
A gossipping-only member (join_ring=false) is stuck in STARTING forever, so the check throws.

## Reproducer extracted
1. Start single node with `-Dcassandra.join_ring=false` (injected via env `JVM_EXTRA_OPTS`).
2. `nodetool disablebinary`  -> binary goes down (CQL refused).
3. `nodetool enablebinary`   -> THROWS on buggy; SUCCEEDS on fixed.

## Environment
- Existing kind cluster (context kind-kind, 4 nodes). Cassandra PODS in kind. No docker run.
- Namespace created by me: `repro-17752` (deleted at teardown).
- Two pods in repro-17752: `cass-buggy` (4.0.5), `cass-ctl` (4.0.6). Single-node each.
- Both started with env `JVM_EXTRA_OPTS=-Dcassandra.join_ring=false`.
- NOTE: images cassandra:4.0.5/4.0.6 could not be pulled from Docker Hub (HTTP 429 rate limit).
  They existed in the host docker store, so I loaded them into all 4 kind nodes via
  `docker save` -> `docker cp <tar> <node>:/cass-40X.tar` -> `ctr -n k8s.io images import --no-unpack`.
  (`kind load docker-image` failed: it exports with `--all-platforms --digests` but the host
  images are single-platform, so it aborted with "content digest ... not found". Recorded in
  tooling_findings; not fixed.)

## Precondition verification (join_ring=false actually took effect — guards vs false negative)

### Buggy 4.0.5 (cass-buggy)
JVM Arguments include: `-Dcassandra.join_ring=false`
Startup log:
```
INFO  [main] StorageService.java:817 - Not joining ring as requested. Use JMX (StorageService->joinRing()) to initiate ring joining
```
`nodetool info`:
```
Gossip active          : true
Native Transport active: true
Token                  : (node is not joined to the cluster)
```
=> gossipping-only member, NOT in NORMAL state, but binary transport is UP (the bug's premise).
`nodetool statusbinary` => running
`cqlsh -e "SELECT now() FROM system.local"` => returns a row (binary reachable).

### Fixed 4.0.6 (cass-ctl) — identical precondition
Startup log:
```
INFO  [main] StorageService.java:817 - Not joining ring as requested. Use JMX (StorageService->joinRing()) to initiate ring joining
```
`nodetool info`:
```
Gossip active          : true
Native Transport active: true
Token                  : (node is not joined to the cluster)
```
Same gossipping-only state.

## RUN — Buggy 4.0.5 (cass-buggy)  [BUG REPRODUCES]
```
$ kubectl exec -n repro-17752 cass-buggy -- nodetool disablebinary
exit=0

$ kubectl exec -n repro-17752 cass-buggy -- cqlsh -e "SELECT now() FROM system.local"
Connection error: ('Unable to connect to any servers', {'127.0.0.1:9042': ConnectionRefusedError(111, "Tried connecting to [('127.0.0.1', 9042)]. Last error: Connection refused")})
command terminated with exit code 1
cqlsh_exit=1

$ kubectl exec -n repro-17752 cass-buggy -- nodetool statusbinary
not running
exit=0

$ kubectl exec -n repro-17752 cass-buggy -- nodetool enablebinary
nodetool: Unable to start native transport because the node is not in the normal state.
See 'nodetool help' or 'nodetool help <command>'.
command terminated with exit code 1
enablebinary_exit=1
```
=> After disablebinary, the binary transport CANNOT be re-enabled. enablebinary throws and the
node is permanently unreachable via CQL. This is the exact symptom in the Jira body.

### VERBATIM BUGGY SIGNATURE
```
nodetool: Unable to start native transport because the node is not in the normal state.
```
(thrown by StorageService#checkServiceAllowedToStart because the gossipping-only member's state is STARTING, not NORMAL)

## RUN — Fixed 4.0.6 (cass-ctl)  [CONTROL — does NOT misbehave]
Identical sequence, identical join_ring=false precondition:
```
$ kubectl exec -n repro-17752 cass-ctl -- nodetool disablebinary
exit=0

$ kubectl exec -n repro-17752 cass-ctl -- nodetool statusbinary
not running
exit=0

$ kubectl exec -n repro-17752 cass-ctl -- nodetool enablebinary
enablebinary_exit=0                         <-- SUCCEEDS (no throw)

$ kubectl exec -n repro-17752 cass-ctl -- nodetool statusbinary
running                                     <-- binary back up

$ kubectl exec -n repro-17752 cass-ctl -- cqlsh -e "SELECT now() FROM system.local"

 system.now()
--------------------------------------
 3f471810-661b-11f1-8bcd-a922d3aab528

(1 rows)
cqlsh_exit=0                                <-- CQL restored
```
=> On 4.0.6 the fix allows enablebinary to succeed for a join_ring=false node; binary transport
is restored and CQL works again. Clean A/B contrast.

## Conclusion
The bug reproduces on the buggy image (4.0.5) and is fixed on the next patch (4.0.6) with the
IDENTICAL workload. Verbatim operator-visible signature captured. Disposition = reproduced.

## A/B Summary
| Step                | Buggy 4.0.5                                              | Fixed 4.0.6           |
|---------------------|---------------------------------------------------------|-----------------------|
| join_ring=false     | yes ("Not joining ring as requested"); Token not joined | same                  |
| binary up at start  | yes (running, cqlsh OK)                                  | same                  |
| disablebinary       | exit 0; cqlsh refused; statusbinary "not running"       | same                  |
| **enablebinary**    | **THROW: "...not in the normal state." exit 1**         | **exit 0 (success)**  |
| statusbinary after  | (stays down)                                            | running               |
| cqlsh after         | (stays refused — node unrecoverable w/o restart)        | returns row           |

## Teardown
`kubectl delete ns repro-17752 --wait=false` (only namespace I created).
Pre-existing namespace `repro-17752-ctl` (from another session) left UNTOUCHED.
