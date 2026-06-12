# CASSANDRA-16146 — Node state incorrectly set to NORMAL after `nodetool disablegossip`+`enablegossip` during bootstrap

- **Buggy version:** cassandra:3.11.9
- **Control (fixed) version:** cassandra:3.11.10 (fixVersions include 3.11.10; within 3.11 ceiling 19)
- **Component:** Cluster/Gossip
- **Disposition:** REPRODUCED
- **Topology used:** 2-node ring (seed NORMAL + joiner held in `write_survey=true`) — matches the classifier `ring` hint.
- **Namespace:** repro-16146 (torn down)
- **Date:** 2026-06-12

## Jira ground truth (primary source)
`/tmp/jira_repro/CASSANDRA-16146.json`:
> At high level, `StorageService#setGossipTokens` set the gossip state to `NORMAL` blindly. Therefore,
> re-enabling gossip (stop and start gossip) overrides the actual gossip state.
> 1. Bootstrap failed. The gossip state remains in `BOOT`/`JOINING` and code execution exits `StorageService#initServer`.
> 2. Operator runs nodetool to stop and re-start gossip. The gossip state gets flipped to `NORMAL`.

## Reproducer extracted
Need a node parked in `BOOT`/`JOINING` (with tokens already saved so `getLocalTokens()` is non-empty),
then run `nodetool disablegossip` + `nodetool enablegossip`. Rather than crash-failing a bootstrap, I used
`-Dcassandra.write_survey=true` on the joiner, which completes streaming but deliberately halts before
becoming an active ring member ("Startup complete, but write survey mode is active, not becoming an active
ring member"). This leaves the joiner in exactly the documented precondition: operation mode `JOINING`,
gossip `STATUS=BOOT`, tokens saved. (A fresh `join_ring=false` node would have NO saved tokens and hit an
AssertionError in `getLocalTokens()` — wrong signature — so write_survey is the faithful path.)

## Fix mechanism (commit fee7a108, the 3.11.10 fix)
The fix adds a guard so `disablegossip`/`enablegossip` are rejected unless the node is NORMAL:
```java
public boolean isNormal() { return operationMode == Mode.NORMAL; }
// in stopGossiping():
if (!isNormal())
    throw new IllegalStateException("Unable to stop gossip because the node is not in the normal state. Try to stop the node instead.");
```
So the buggy 3.11.9 (no guard) lets the operator flip a BOOT/JOINING node to NORMAL; the fixed 3.11.10
refuses the operation, preventing the bad transition.

=========================================================
## BUGGY RUN — cassandra:3.11.9 (REPRODUCED)

Topology: cass-seed (3.11.9, NORMAL) + cass-joiner (3.11.9, `JVM_EXTRA_OPTS=-Dcassandra.write_survey=true`,
`CASSANDRA_SEEDS=<seed IP>`). Joiner IP 10.244.2.123, seed IP 10.244.2.122.

Joiner log proving the precondition:
```
INFO  StorageService.java:1549 - Bootstrap completed for tokens [...]
INFO  StorageService.java:1080 - Startup complete, but write survey mode is active, not becoming an active ring member. Use JMX (StorageService->joinRing()) to finalize ring joining.
```

### BEFORE the dance (buggy)
```
$ kubectl exec -n repro-16146 cass-joiner -- nodetool netstats | head -1
Mode: JOINING

$ kubectl exec -n repro-16146 cass-joiner -- nodetool gossipinfo   # self entry
/10.244.2.123
  STATUS:24:BOOT,-2177934520239161152

$ kubectl exec -n repro-16146 cass-seed -- nodetool status
UN  10.244.2.122  75.71 KiB  256  100.0%  a17263ac-70dc-4306-8595-fc3e08120011  rack1
UJ  10.244.2.123  31.4 KiB   256  ?       05235ba2-90d3-4689-8398-b261ba8b74f5  rack1
```

### THE DANCE (buggy) — both succeed (NO guard)
```
$ kubectl exec -n repro-16146 cass-joiner -- nodetool disablegossip ; echo exit=$?
exit=0
$ kubectl exec -n repro-16146 cass-joiner -- nodetool enablegossip ; echo exit=$?
exit=0
```
Joiner server log:
```
WARN  StorageService.java:332 - Stopping gossip by operator request
WARN  StorageService.java:336 - Disabling gossip while native transport is still active is unsafe
WARN  Gossiper.java:1670 - No local state, state is in silent shutdown, or node hasn't joined, not announcing shutdown
WARN  StorageService.java:351 - Starting gossip by operator request
```

### AFTER the dance (buggy) — THE BUG
```
$ kubectl exec -n repro-16146 cass-joiner -- nodetool netstats | head -1
Mode: JOINING                                  <-- node itself STILL JOINING

$ kubectl exec -n repro-16146 cass-joiner -- nodetool gossipinfo   # self entry
/10.244.2.123
  STATUS:87:NORMAL,-1077568207160367180        <-- gossip STATUS BLINDLY FLIPPED BOOT -> NORMAL

$ kubectl exec -n repro-16146 cass-seed -- nodetool status
UN  10.244.2.122  75.71 KiB  256  100.0%  a17263ac-70dc-4306-8595-fc3e08120011  rack1
UN  10.244.2.123  31.4 KiB   256  100.0%  05235ba2-90d3-4689-8398-b261ba8b74f5  rack1   <-- UJ -> UN
```

**Buggy signature:** the joiner advertises `STATUS:87:NORMAL,-1077568207160367180` (was `BOOT`) after the
disablegossip/enablegossip dance, while its own operation mode is still `Mode: JOINING`. The rest of the
ring (seed `nodetool status`) consequently flips the joiner from `UJ` to `UN`, i.e. a node that never
finished joining now reads as a Normal ring member eligible for reads/writes — exactly the Jira symptom
"the gossip state gets flipped to NORMAL".

=========================================================
## CONTROL RUN — cassandra:3.11.10 (FIX prevents the bad transition)

Identical topology/workload: cass-seed (3.11.10, NORMAL) + cass-joiner (3.11.10, write_survey).
Joiner IP 10.244.2.126, seed IP 10.244.2.124.

### BEFORE the dance (control) — same precondition as buggy
```
$ nodetool netstats | head -1   ->  Mode: JOINING
$ nodetool gossipinfo (self)    ->  /10.244.2.126
                                      STATUS:32:BOOT,1572982465826127769
$ (seed) nodetool status        ->  UJ  10.244.2.126 ...   (joiner = Up/Joining)
```

### THE DANCE (control) — disablegossip REFUSED by the fix
```
$ kubectl exec -n repro-16146 cass-joiner -- nodetool disablegossip ; echo exit=$?
nodetool: Unable to stop gossip because the node is not in the normal state. Try to stop the node instead.
See 'nodetool help' or 'nodetool help <command>'.
command terminated with exit code 1
exit=1
```

### AFTER (control) — no flip, state preserved
```
$ nodetool gossipinfo (self)  ->  /10.244.2.126
                                    STATUS:32:BOOT,1572982465826127769   <-- STILL BOOT (unchanged)
$ (seed) nodetool status      ->  UJ  10.244.2.126 ...                   <-- STILL UJ (not flipped)
```

**Control conclusion:** on the fixed image the operator simply cannot disable gossip on a non-NORMAL node
(`IllegalStateException: Unable to stop gossip because the node is not in the normal state.`), so the
BOOT->NORMAL flip never happens. The A/B difference is solely the buggy code path described in the Jira.

## tag_correction
Hint said `topology=ring, confidence=M`, trigger "bootstrap fails ... disablegossip+enablegossip ->
setGossipTokens blindly flips state to NORMAL". Confirmed accurate — ring is correct and the mechanism is
exactly as described. The only refinement: instead of a *crashed* bootstrap I parked the joiner in the
same BOOT/JOINING precondition via `write_survey=true` (cleaner + deterministic, and keeps tokens saved so
no spurious AssertionError). Mechanism and observable outcome are faithful to the body.

## Teardown
Deleted pods then `kubectl delete ns repro-16146 --wait=false`.
