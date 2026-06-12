# CASSANDRA-16127 — NullPointerException when calling nodetool enablethrift

- **Buggy version:** cassandra:3.11.8 (pod `cass`)
- **Fixed-control version:** cassandra:3.11.9 (pod `cass-ctl`)  [fix versions: 2.2.19, 3.0.23, **3.11.9**; 9 <= 3.11 ceiling 19]
- **Namespace:** repro-16127 (kind cluster, context kind-kind)
- **Topology:** 1 node (HINT topology=1node confirmed by body — single-node JMX/nodetool bug, no ring needed)
- **Component:** Messaging/Thrift
- **Disposition:** NOT-REPRODUCIBLE on the released docker image (precondition shadowed by full daemon boot). The null-deref code path is genuinely present in 3.11.8, but it cannot be reached via `nodetool` on a normally-booted pod; reproduction requires controlling `CassandraDaemon` lifecycle ordering (in-JVM dtest), as the fix's own validation confirms.

## Jira body (ground truth reproducer)
> Having thrift disabled, it's impossible to enable it again without restarting the node:
> ```
> $ nodetool statusthrift
> not running
> $ nodetool enablethrift
> error: null
> -- StackTrace --
> java.lang.NullPointerException
>     at org.apache.cassandra.service.StorageService.startRPCServer(StorageService.java:392)
>     ...
> ```

## HINT vs reality (tag_correction)
Classifier hint trigger: "nodetool disablethrift then enablethrift -> NPE". The disable->enable cycle does NOT
reproduce on the released cassandra:3.11.8 image (see below). The body's real precondition is "thrift was never
started" i.e. `daemon.thriftServer == null`, which on a fully-booted node is never true.

## Root cause (from 3.11.8 source + fix commit 3ee90cfc94ee038b7758a57b56d3ec09b514cb88)
- `StorageService.startRPCServer()` ends with `daemon.thriftServer.start();` after only checking `daemon != null`.
  It does NOT null-check `daemon.thriftServer`. NPE if `daemon.thriftServer == null`.
- `CassandraDaemon.thriftServer` is constructed unconditionally inside `initializeNativeTransport()`
  (`if (thriftServer == null) thriftServer = new ThriftServer(...)`). `start_rpc:false` only skips the
  subsequent `thriftServer.start()` call, NOT the construction.
- Therefore the NPE requires `enablethrift` to run BEFORE `initializeNativeTransport()` has ever executed.
- The fix (3.11.9) makes `thriftServer` `volatile`, renames init to `initializeClientTransports()`, and routes
  `StorageService` through new null-safe `daemon.startThriftServer()` which asserts "setup() must be called first
  for CassandraDaemon". It was validated with an **in-JVM dtest** (controls daemon lifecycle), not a live-node test.
- Duplicate CASSANDRA-16091 ("rpc server gets wrongly initialized with rpc_enabled:false") corroborates: the bug
  is about transport-init ordering, not a simple disable->enable cycle.

## Why it does NOT fire on the released image (evidence)
On `cassandra:3.11.8` the docker entrypoint runs a full `CassandraDaemon.setup()`, which calls
`initializeNativeTransport()`, constructing `thriftServer` at boot (proven by boot log below). Hence
`daemon.thriftServer` is always non-null on a booted pod, and `disablethrift` only *stops* the server (it does
not null the reference — see `stopRPCServer()` which keeps the field). So re-enabling always succeeds.

### Buggy 3.11.8 boot log (verbatim, proves transport init ran)
```
INFO  [main] ... StorageService.java:664 - Thrift API version: 20.1.0
INFO  [main] ... ThriftServer.java:116 - Binding thrift service to /0.0.0.0:9160
INFO  [Thread-2] ... ThriftServer.java:133 - Listening for thrift clients...
INFO  [main] ... CassandraDaemon.java:548 - Not starting RPC server as requested. Use JMX (StorageService->startRPCServer()) or nodetool (enablethrift) to start it
```
`grep -c "Not starting RPC server as requested"` on buggy boot = 1  (so initializeNativeTransport executed and
thriftServer was constructed). cassandra.yaml on the pod has `start_rpc: false` (line 675); CASSANDRA_START_RPC unset.

## Commands run + raw outputs

### Deploy (both pods Ready, CQL up ~20s each)
```
kubectl create namespace repro-16127
# Pod cass = cassandra:3.11.8 ; Pod cass-ctl = cassandra:3.11.9 (single-node template)
kubectl wait -n repro-16127 --for=condition=Ready pod/cass pod/cass-ctl --timeout=300s
# -> pod/cass condition met ; pod/cass-ctl condition met
```

### BUGGY 3.11.8 (pod cass) — reporter's exact sequence, run twice
```
$ nodetool version          -> ReleaseVersion: 3.11.8
$ nodetool statusthrift      -> running            # image boots thrift ON (initializeNativeTransport ran)
$ nodetool disablethrift     -> rc=0
$ nodetool statusthrift      -> not running        # precondition matches body
$ nodetool enablethrift      -> rc=0  OUTPUT:[]     # NO NPE, empty output, success
$ nodetool statusthrift      -> running
```
A first attempt before any disable (cold `enablethrift` while already "running") also returned rc=0 with no error.

### CONTROL 3.11.9 (pod cass-ctl) — identical sequence
```
$ nodetool version          -> ReleaseVersion: 3.11.9
$ nodetool statusthrift      -> not running         # 3.11.9 boots thrift OFF (different boot behavior)
$ nodetool enablethrift      -> rc=0
$ nodetool statusthrift      -> running
$ nodetool disablethrift     -> rc=0
$ nodetool statusthrift      -> not running
$ nodetool enablethrift      -> rc=0
$ nodetool statusthrift      -> running
```
Control behaves correctly (as expected for the fixed version).

## Verbatim buggy signature
NONE OBSERVED. The reporter's `error: null` / `java.lang.NullPointerException at
org.apache.cassandra.service.StorageService.startRPCServer` did NOT appear; `nodetool enablethrift` returned
rc=0 with empty output on cassandra:3.11.8. Per the evidence bar, with no verbatim buggy signature this is NOT
"reproduced".

## Disposition rationale
not-reproducible: the body's mechanism (null `daemon.thriftServer`) does not fire on the released
cassandra:3.11.8 image because the docker boot always runs `initializeNativeTransport()` and constructs
`thriftServer`. Reaching the null state requires invoking `startRPCServer` before daemon transport
initialization — i.e. controlling `CassandraDaemon` lifecycle, which the fix itself exercised via an in-JVM
dtest. This is not stageable on a live booted pod via nodetool. (Closely adjacent to "confirmed-blocked: needs
in-JVM daemon lifecycle control"; chosen not-reproducible because we ran the body's literal reproducer on the
buggy image and showed with evidence that it does not fire.)
