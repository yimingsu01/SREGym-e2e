# CASSANDRA-18935 — Reproduction Evidence

**Summary (Jira):** Fix nodetool enable/disablebinary to correctly set rpc
**Buggy version:** cassandra:4.1.3
**Fixed control:** cassandra:4.1.4 (fixVersions include 4.1.4; 4.1.4 <= line ceiling 11)
**Components:** Legacy/Core, Legacy/CQL
**Namespace:** repro-18935 (kind-kind)   |   Keyspace: repro18935_ks
**Disposition:** REPRODUCED (with A/B control)

## Bug mechanism (from Jira body — ground truth)
Startup code in CassandraDaemon:
```java
if ((nativeFlag != null && Boolean.parseBoolean(nativeFlag)) || (nativeFlag == null && DatabaseDescriptor.startNativeTransport())) {
    startNativeTransport();
    StorageService.instance.setRpcReady(true);
}
```
The startup code only sets `RpcReady=true` if native transport is enabled at startup. If you start with
native transport OFF and later run `nodetool enablebinary`, native transport starts but `setRpcReady(true)`
is never called. Since CASSANDRA-13043, a counter update requires `RpcReady=true` to select a leader, so the
counter update fails to find any "alive" replica.

## Reproducer extracted
1. Start a single Cassandra node with native (binary) transport OFF.
2. `nodetool enablebinary`  -> native transport starts, but RpcReady stays false (the bug).
3. A counter UPDATE fails because the counter-leader logic sees no RPC-ready replica.
   (A plain non-counter write still succeeds — proving the node is otherwise healthy.)

Classifier hint matched the body exactly (topology=1node, confidence=H). tag_correction: none.

## Gating mechanism used
Env var on the pod: `JVM_EXTRA_OPTS=-Dcassandra.start_native_transport=false`
This maps directly to the `nativeFlag = System.getProperty("cassandra.start_native_transport")` in the bug's
own code snippet. cassandra.yaml keeps `start_native_transport: true`, so CassandraDaemon.setup() still
constructs the nativeTransportService (required for `enablebinary` to work), while start() skips the
`startNativeTransport(); setRpcReady(true);` branch.

### Gating PROOF (native transport really OFF at startup, both pods)
```
$ kubectl exec -n repro-18935 cass-buggy   -- nodetool statusbinary
not running
$ kubectl exec -n repro-18935 cass-control -- nodetool statusbinary
not running
```
Startup log line (buggy pod) confirming the path and that enablebinary is the intended re-enable mechanism:
```
INFO [main] ... CassandraDaemon.java:695 - Not starting native transport as requested.
Use JMX (StorageService->startNativeTransport()) or nodetool (enablebinary) to start it
```

## BUGGY RUN — cassandra:4.1.3  (pod cass-buggy)
```
$ kubectl exec -n repro-18935 cass-buggy -- nodetool enablebinary      # clean (no error)
$ kubectl exec -n repro-18935 cass-buggy -- nodetool statusbinary
running

# Contrast: plain (non-counter) write SUCCEEDS — node is healthy for normal writes
$ cqlsh -e "INSERT INTO repro18935_ks.plain (k,v) VALUES ('a','hello'); SELECT * FROM repro18935_ks.plain;"
 k | v
---+-------
 a | hello
(1 rows)

# >>>> COUNTER UPDATE (BUG TRIGGER) <<<<
$ cqlsh -e "UPDATE repro18935_ks.cnt SET c = c + 1 WHERE k = 'a';"
<stdin>:1:NoHostAvailable: ('Unable to complete the operation against any hosts', {<Host: 127.0.0.1:9042 dc1>: Unavailable('Error from server: code=1000 [Unavailable exception] message="Cannot achieve consistency level ONE" info={\'consistency\': \'ONE\', \'required_replicas\': 1, \'alive_replicas\': 0}')})
command terminated with exit code 2
```

### VERBATIM BUGGY SIGNATURE (literal cqlsh output, backslash-escaped quotes preserved)
```
<stdin>:1:NoHostAvailable: ('Unable to complete the operation against any hosts', {<Host: 127.0.0.1:9042 dc1>: Unavailable('Error from server: code=1000 [Unavailable exception] message="Cannot achieve consistency level ONE" info={\'consistency\': \'ONE\', \'required_replicas\': 1, \'alive_replicas\': 0}')})
```
Note `alive_replicas: 0` while `required_replicas: 1` on a healthy single-node ring with RF=1: no replica is
counted as RPC-ready for counter-leader selection — the precise symptom of RpcReady never being set.

## CONTROL RUN — cassandra:4.1.4 (FIXED)  (pod cass-control)
Identical config (same JVM_EXTRA_OPTS) and identical command sequence.
```
$ kubectl exec -n repro-18935 cass-control -- nodetool statusbinary   # gating proof
not running
$ kubectl exec -n repro-18935 cass-control -- nodetool enablebinary   # clean
$ kubectl exec -n repro-18935 cass-control -- nodetool statusbinary
running

# >>>> SAME COUNTER UPDATE — SUCCEEDS on fixed 4.1.4 <<<<
$ cqlsh -e "UPDATE repro18935_ks.cnt SET c = c + 1 WHERE k = 'a'; SELECT * FROM repro18935_ks.cnt;"
 k | c
---+---
 a | 1
(1 rows)
command exit code: 0
```

## Conclusion
A/B is decisive: with the IDENTICAL gating config and IDENTICAL command sequence, the counter UPDATE fails on
buggy 4.1.3 (`Unavailable / Cannot achieve consistency level ONE / alive_replicas: 0`) and succeeds on fixed
4.1.4 (returns c=1). The fix (move `setRpcReady(true)` out of the start-native-transport `if`) corrects the
behavior. Bug CASSANDRA-18935 REPRODUCED.

## Teardown
`kubectl delete ns repro-18935 --wait=false`
