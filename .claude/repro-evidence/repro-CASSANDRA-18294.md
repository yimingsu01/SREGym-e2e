# CASSANDRA-18294 — "die disk failure policy will not kill jvm as documented"

- **Buggy version:** cassandra:4.1.0 (deployed as a single pod in kind ns `repro-18294`)
- **Fix versions (Jira):** 3.0.29, 4.0.9, 4.1.1, 5.0-alpha1, 5.0
- **Component:** Local/Config
- **Classifier hint:** topology=1node, confidence=M, trigger="disk_failure_policy=die + inject filesystem
  error -> Cassandra only throws instead of shutting down gossip/transport and killing the JVM"
- **DISPOSITION: not-reproducible** (the body's mechanism is *shadowed* on the buggy image — see below).

## 1. Reproducer extracted from the Jira body
The body states: after Cassandra has successfully started with `disk_failure_policy: die`, when it encounters
a filesystem error at runtime, the server should "shut down gossip and client transport and kill JVM" but
instead "will only throw exception". Root cause per reporter: "the default FS error handler is not handling
policy die correctly. Instead of shutting down gossip and native transport, it throws an error."

Derived reproducer: bring a node fully UP with `disk_failure_policy: die`, then inject a runtime FSError
(make a table's data dir inaccessible, INSERT a fresh row, `nodetool flush`) and observe whether the buggy
`DefaultFSErrorHandler.handleFSError` throws `IllegalStateException` while leaving the JVM/transports up.

## 2. Source-level confirmation of the code defect (real, but shadowed)
Verbatim from `src/java/org/apache/cassandra/service/DefaultFSErrorHandler.java`:

- **cassandra-4.1.0 `handleFSError()` switch** has cases: `stop_paranoid`, `stop`, `best_effort`, `ignore`,
  and `default: throw new IllegalStateException();`  — **there is NO `case die:`**. So in isolation, calling
  `handleFSError` with policy `die` falls to `default:` and throws `java.lang.IllegalStateException`.
- **cassandra-4.1.1** adds `case die:` grouped with `stop_paranoid`/`stop`:
  ```
  case die:
  case stop_paranoid:
  case stop:
      logger.error("Stopping transports as disk_failure_policy is " + DatabaseDescriptor.getDiskFailurePolicy());
      StorageService.instance.stopTransports();
      break;
  ```
  (Note: the fix makes `die` behave like `stop_paranoid` — stop transports — it does NOT actually
  `System.exit` the JVM in `handleFSError`, contrary to the doc/title wording.)

## 3. WHY the defect does not produce a runtime client/operator-visible symptom on 4.1.0
Caller map (shallow clone of cassandra-4.1.0):
- The ONLY direct (non-propagate) caller of `FileUtils.handleFSError` is `JVMStabilityInspector.inspectDiskError`
  (JVMStabilityInspector.java:100), which is the `fn` consumer invoked inside `inspectThrowable`.
- ALL runtime FSError producers (LogReplica, LogReplicaSet, LogTransaction, Hints*) call
  `FileUtils.handleFSErrorAndPropagate(e)`, which is:
  ```
  public static void handleFSErrorAndPropagate(FSError e) {
      JVMStabilityInspector.inspectThrowable(e);   // <-- runs FIRST
      throw propagate(e);
  }
  ```
- `JVMStabilityInspector.inspectThrowable(t, fn)`:
  ```
  if (DatabaseDescriptor.getDiskFailurePolicy() == Config.DiskFailurePolicy.die)
      if (t instanceof FSError || t instanceof CorruptSSTableException)
          isUnstable = true;
  ...
  if (isUnstable) {
      if (!isDaemonSetupCompleted()) FileUtils.handleStartupFSError(t);
      killer.killCurrentJVM(t);        // <-- Killer.killCurrentJVM -> System.exit(100); never returns
  }
  try { fn.accept(t); } ...            // <-- inspectDiskError -> handleFSError (buggy switch) is UNREACHABLE for die
  ```
- `Killer.killCurrentJVM` ends in `StorageService.instance.removeShutdownHook(); System.exit(100);`.

=> For `disk_failure_policy: die`, the JVM is killed by `inspectThrowable` BEFORE the buggy `handleFSError`
switch is ever reached. The missing `die` case (and its `IllegalStateException`) is therefore **dead/shadowed
code at runtime**. The documented behavior (kill JVM) actually happens.

The fix's own unit test `DefaultFSErrorHandlerTest.testFSErrors` (4.1.1) confirms this: it calls
`handler.handleFSError(new FSReadError(new IOException(), "blah"))` **directly**, deliberately bypassing
`inspectThrowable`, and its `@BeforeClass` comment states: "startup must be completed, otherwise FS error
will kill JVM regardless of failure policy." i.e. the normal path kills the JVM; the test must bypass it to
exercise the buggy switch.

## 4. Empirical reproduction attempt on the BUGGY 4.1.0 image (kind ns repro-18294)
Pod template: cassandra:4.1.0, single node, `disk_failure_policy: die` set via
`sed -i 's/^disk_failure_policy:.*/disk_failure_policy: die/' /etc/cassandra/cassandra.yaml` before
`exec docker-entrypoint.sh cassandra -f`.

Verified policy + node up:
```
$ kubectl exec -n repro-18294 cass -- grep '^disk_failure_policy' /etc/cassandra/cassandra.yaml
disk_failure_policy: die
$ kubectl exec -n repro-18294 cass -- nodetool status        # UN  10.244.2.100 ... rack1
$ kubectl exec -n repro-18294 cass -- nodetool info | grep -i 'native transport\|gossip'
Gossip active          : true
Native Transport active: true
$ kubectl exec -n repro-18294 cass -- cqlsh -e "SELECT now() FROM system.local"   # returns a timeuuid
```
Config.java log line confirms runtime config: `disk_failure_policy=die`.

Trigger (write path):
```
CREATE KEYSPACE repro18294 WITH replication={'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE repro18294.t (id int PRIMARY KEY, v text);
INSERT INTO repro18294.t (id,v) VALUES (1,'first');  nodetool flush repro18294   # creates t-<uuid> dir
# table dir now exists: /var/lib/cassandra/data/repro18294/t-4cbcb4c0662311f19e51b1b044ec7e05
kubectl exec -n repro-18294 cass -- chmod 000 /var/lib/cassandra/data/repro18294/t-4cbcb4c0662311f19e51b1b044ec7e05
INSERT INTO repro18294.t (id,v) VALUES (2,'after-chmod');                        # succeeds (memtable+commitlog)
kubectl exec -n repro-18294 cass -- nodetool flush repro18294                    # -> exit 137 (JVM killed)
```

### OBSERVED RESULT = the documented (correct) `die` behavior, which REFUTES the report's symptom
`kubectl logs -n repro-18294 cass --previous` (the container that ran the flush) tail:
```
ERROR [MemtableFlushWriter:1] 2026-06-12 05:55:37,410 JVMStabilityInspector.java:254 - JVM state determined to be unstable.  Exiting forcefully due to:
org.apache.cassandra.io.FSReadError: java.io.IOException: Invalid folder descriptor trying to create log replica /var/lib/cassandra/data/repro18294/t-4cbcb4c0662311f19e51b1b044ec7e05
	at org.apache.cassandra.db.lifecycle.LogReplica.create(LogReplica.java:61)
	at org.apache.cassandra.db.lifecycle.LogReplicaSet.maybeCreateReplica(LogReplicaSet.java:87)
	at org.apache.cassandra.db.lifecycle.LogFile.maybeCreateReplica(LogFile.java:353)
	... (flush stack) ...
```
- `nodetool flush` exited 137 (SIGKILL = JVM `System.exit(100)` via `Killer.killCurrentJVM`).
- Pod RestartCount went 0 -> 1 (kubelet restarted the container after the JVM exit).
- `grep -c IllegalStateException` over the previous container log = **0**. The buggy
  `DefaultFSErrorHandler.handleFSError` default branch was NEVER hit — the FSError was routed through
  `JVMStabilityInspector` which killed the JVM, i.e. the *documented* behavior.

There is therefore NO buggy `IllegalStateException`-from-`handleFSError` signature obtainable on a real node
via runtime FS errors; instead we get the correct kill. This is positive empirical evidence that the body's
mechanism is shadowed on the buggy image.

## 5. A/B control (within-version reasoning; 4.1.1 deploy intentionally skipped)
The fix (4.1.1) modifies ONLY `DefaultFSErrorHandler.handleFSError`/`handleStartupFSError`; it does NOT touch
`JVMStabilityInspector.inspectThrowable` or `handleFSErrorAndPropagate`. Since the runtime kill happens in
`inspectThrowable` BEFORE `handleFSError` on both 4.1.0 and 4.1.1, the runtime behavior of the identical
workload is the same on both images (JVM killed). The fix only changes what happens when `handleFSError` is
invoked directly (the unit-test path), where 4.1.0 throws `IllegalStateException` and 4.1.1 stops transports.
Deploying 4.1.1 would show the same kill on flush — no client-visible A/B delta — so the cycle was not spent.

## 6. Conclusion
- The code defect is REAL (missing `case die:` in 4.1.0 `handleFSError`), but it is unreachable at runtime for
  `disk_failure_policy: die`: every runtime FSError is intercepted by `JVMStabilityInspector.inspectThrowable`
  which kills the JVM first. The documented "kill JVM" behavior actually occurs (observed: exit 137 +
  "Exiting forcefully").
- The ONLY way to exercise the buggy branch is the fix's own unit test calling `handleFSError` directly
  (bypassing inspectThrowable) — a needs-fix-test situation — but we have positive empirical evidence of the
  shadowing on the real image, so the primary disposition is **not-reproducible**.
- tag_correction: the hint trigger ("Cassandra only throws instead of shutting down/killing the JVM") is
  contradicted on the buggy image: the JVM IS killed at runtime; the throw-path is shadowed by inspectThrowable.

## Environment / teardown
- Namespace created: repro-18294 (single pod `cass`, cassandra:4.1.0). Keyspace: repro18294.
- Other namespaces (repro-14113, repro-16071, repro-18264, cert-manager, k8ssandra-operator) NOT touched.
- Teardown: `kubectl delete ns repro-18294 --wait=false`; `rm -rf /tmp/cass410src`.
