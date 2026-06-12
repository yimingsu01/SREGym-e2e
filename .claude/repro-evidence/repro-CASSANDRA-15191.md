# CASSANDRA-15191 reproduction

**Summary:** stop_paranoid disk failure policy is ignored on CorruptSSTableException after node is up
**Buggy version:** cassandra:3.11.7 | **Fixed control:** cassandra:3.11.8 (in fixVersions: 3.0.22, 3.11.8, 4.0-beta2, 4.0)
**Components:** Local/Config | **Topology:** 1 node | **Namespace:** repro-15191 (buggy), repro-15191-ctl (control)
**Disposition: REPRODUCED**

## Bug (from Jira body)
When `disk_failure_policy=stop_paranoid` and a `CorruptSSTableException` is thrown AFTER the server is up
(e.g. on a regular SELECT), the policy is IGNORED. It should stop gossip + transport but instead just logs
the exception and keeps serving. Root cause (per body): the exception thrown in
`AbstractLocalAwareExecutorService` is a `RuntimeException` with `CorruptSSTableException` as its *cause*,
so the policy check does not recognize it.

Tag check: classifier hint (topology=1node; trigger=set stop_paranoid + corrupt sstable + SELECT ->
policy ignored, node keeps serving) MATCHES the body exactly. tag_correction = none.

## Discriminating signal
The read fails (ReadFailure) on BOTH versions because the sstable is corrupt either way -- that is NOT the
signature. The bug is NODE LIVENESS after the corrupt read:
- 3.11.7 (buggy): gossip + binary stay RUNNING, node keeps serving  => policy ignored.
- 3.11.8 (fixed): gossip + binary go NOT RUNNING (JVM stays up for JMX investigation).

## Reproducer (exact steps, both versions identical)
1. Single pod, image cassandra:<VER>, with cassandra.yaml edited at startup:
   `sed -i 's/^disk_failure_policy:.*/disk_failure_policy: stop_paranoid/'` and append `disk_access_mode: standard`.
   (disk_access_mode=standard forces buffered reads so the LZ4 per-chunk CRC is re-validated from disk;
    with the default mmap mode the corrupted bytes were served from page cache and no exception fired --
    the first attempt on 3.11.7/mmap returned all 2000 rows. This is an environment caveat, not the bug.)
2. `CREATE KEYSPACE repro15191 ... SimpleStrategy rf=1; CREATE TABLE repro15191.t (id int PRIMARY KEY, payload text);`
   (default LZ4 compression kept -- the per-chunk CRC is what raises CorruptSSTableException)
3. INSERT 2000 rows (~400-byte payload each).
4. `nodetool flush repro15191`  (data on disk).
5. Corrupt the body of the Data.db: `dd if=/dev/urandom of=<Data.db> bs=1 count=8000 seek=200 conv=notrunc`.
6. TRIGGER: `SELECT * FROM repro15191.t;`  (full scan hits the corrupt chunk).
7. Observe node liveness: `nodetool statusgossip`, `nodetool statusbinary`, and a fresh `SELECT now()`.

---

## BUGGY 3.11.7 -- raw evidence (namespace repro-15191)

### Config confirmed at startup
```
disk_failure_policy: stop_paranoid
disk_access_mode: standard
INFO  [main] ... DatabaseDescriptor.java:392 - DiskAccessMode is standard, indexAccessMode is standard
ReleaseVersion: 3.11.7
```

### Baseline before corruption: gossip running, binary running.

### Trigger output (cqlsh):
```
<stdin>:1:ReadFailure: Error from server: code=1300 [Replica(s) failed to execute read] message="Operation failed - received 0 responses and 1 failures" info={'failures': 1, 'received_responses': 0, 'required_responses': 1, 'consistency': 'ONE'}
command terminated with exit code 2
```

### Server log -- ROOT-CAUSE SIGNATURE (RuntimeException wrapping CorruptSSTableException as its cause):
```
java.lang.RuntimeException: org.apache.cassandra.io.sstable.CorruptSSTableException: Corrupted: /var/lib/cassandra/data/repro15191/t-cde16de0661711f1940b7749bc9e2758/md-1-big-Data.db
	at org.apache.cassandra.service.StorageProxy$DroppableRunnable.run(StorageProxy.java:2656) ~[apache-cassandra-3.11.7.jar:3.11.7]
	at java.util.concurrent.Executors$RunnableAdapter.call(Executors.java:511) ~[na:1.8.0_262]
	at org.apache.cassandra.concurrent.AbstractLocalAwareExecutorService$FutureTask.run(AbstractLocalAwareExecutorService.java:165) ~[apache-cassandra-3.11.7.jar:3.11.7]
	at org.apache.cassandra.concurrent.AbstractLocalAwareExecutorService$LocalSessionFutureTask.run(AbstractLocalAwareExecutorService.java:137) [apache-cassandra-3.11.7.jar:3.11.7]
	at org.apache.cassandra.concurrent.SEPWorker.run(SEPWorker.java:113) [apache-cassandra-3.11.7.jar:3.11.7]
	at java.lang.Thread.run(Thread.java:748) [na:1.8.0_262]
Caused by: org.apache.cassandra.io.sstable.CorruptSSTableException: Corrupted: /var/lib/cassandra/data/repro15191/t-cde16de0661711f1940b7749bc9e2758/md-1-big-Data.db
	at org.apache.cassandra.io.sstable.format.big.BigTableScanner$KeyScanningIterator.computeNext(BigTableScanner.java:405) ~[apache-cassandra-3.11.7.jar:3.11.7]
	...
```
This is EXACTLY the body's described root cause: thrown is RuntimeException, CorruptSSTableException is the
cause, frame is AbstractLocalAwareExecutorService.

### KEY EVIDENCE -- policy IGNORED (node alive and serving AFTER the corrupt read):
```
statusgossip: running
statusbinary: running
SELECT now() FROM system.local =>
 system.now()
--------------------------------------
 db878a10-6617-11f1-940b-7749bc9e2758
```
Shutdown lines in log ("Stopping gossiper" / "Stopping native transport" / DiskFailure killer): ZERO.
=> stop_paranoid had no effect; node continued serving. BUG REPRODUCED.

---

## CONTROL (fixed 3.11.8) -- to be filled below
