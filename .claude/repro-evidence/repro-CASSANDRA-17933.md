# CASSANDRA-17933 — Reproduction Evidence Log

**Title:** Zero length file in Audit log folder, prevents a node from starting
**Buggy version:** cassandra:4.0.6  |  **Fixed control:** cassandra:4.0.7
**Fix versions (Jira):** 4.0.7, 4.1-rc1, 4.1, 5.0-alpha1, 5.0
**Component:** Local/Startup and Shutdown
**Topology:** single node (1node) — config-gated (audit logging must be enabled)
**Namespace:** repro-17933 (context kind-kind)
**Disposition:** REPRODUCED

---

## 1. Exact reproducer extracted from the Jira body

From the description (ground truth):
> "We have encountered a 4.0.3 cluster where the audit log folder had a zero byte length file within it
> after the node had stopped... On restarting the node, the node will not start and throws the following
> stack trace [ExceptionInInitializerError -> ConfigurationException: Unable to create instance of
> IAuditLogger -> OverlappingFileLockException]."
> "To reproduce, we place a zero length file and attempted to start the node, and saw the same stack trace."
> `-rw-rw-r--. 1 automaton automaton 0 Sep 28 13:00 20220928-12.cq4`

Steps:
1. Enable audit logging (`audit_logging_options.enabled: true`, default logger BinAuditLogger).
2. Place a **zero-byte** `.cq4` file in the audit log directory.
3. Start the node -> startup aborts with `OverlappingFileLockException` thrown from chronicle-queue's
   `SingleChronicleQueue.cleanupStoreFilesWithNoData` while initializing `BinAuditLogger`/`BinLog`.

Classifier hint ("Place a zero-byte .cq4 file in the audit log dir + start node -> startup fails with
OverlappingFileLockException", topology=1node, confidence=H) MATCHES the body exactly. tag_correction: none.

## 2. Buggy deploy (cassandra:4.0.6)

Single pod (`/tmp/repro-17933-buggy.yaml`). Container command, before launching Cassandra:
- patches cassandra.yaml: `audit_logging_options: { enabled: true, audit_logs_dir: /var/lib/cassandra/audit }`
- `mkdir -p /var/lib/cassandra/audit` ; `: > /var/lib/cassandra/audit/20220928-12.cq4`  (zero-byte trigger)
- `exec docker-entrypoint.sh cassandra -f`

Verified pre-start state (from pod log):
```
=== patched audit_logging_options ===
1334-    enabled: true
1335-    audit_logs_dir: /var/lib/cassandra/audit
=== audit dir contents before start ===
-rw-r--r-- 1 root      root         0 Jun 12 02:56 20220928-12.cq4
```
Config line confirming audit logging is ON:
```
audit_logging_options=AuditLogOptions{enabled=true, logger='BinAuditLogger', ..., audit_logs_dir='/var/lib/cassandra/audit', ..., roll_cycle='HOURLY', block=true, ...}
```

## 3. BUGGY SIGNATURE (verbatim from `kubectl logs cass -n repro-17933`)

```
ERROR [main] 2026-06-12 02:58:53,918 CassandraDaemon.java:911 - Exception encountered during startup
java.lang.ExceptionInInitializerError: null
	at org.apache.cassandra.service.CassandraDaemon.setup(CassandraDaemon.java:468)
	at org.apache.cassandra.service.CassandraDaemon.activate(CassandraDaemon.java:765)
	at org.apache.cassandra.service.CassandraDaemon.main(CassandraDaemon.java:889)
Caused by: org.apache.cassandra.exceptions.ConfigurationException: Unable to create instance of IAuditLogger.
	at org.apache.cassandra.utils.FBUtilities.newAuditLogger(FBUtilities.java:686)
	at org.apache.cassandra.audit.AuditLogManager.getAuditLogger(AuditLogManager.java:95)
	at org.apache.cassandra.audit.AuditLogManager.<init>(AuditLogManager.java:74)
	at org.apache.cassandra.audit.AuditLogManager.<clinit>(AuditLogManager.java:60)
	... 3 common frames omitted
Caused by: java.lang.reflect.InvocationTargetException: null
	at java.base/jdk.internal.reflect.NativeConstructorAccessorImpl.newInstance0(Native Method)
	...
	at org.apache.cassandra.utils.FBUtilities.newAuditLogger(FBUtilities.java:682)
	... 6 common frames omitted
Caused by: java.nio.channels.OverlappingFileLockException: null
	at java.base/sun.nio.ch.FileLockTable.checkList(Unknown Source)
	at java.base/sun.nio.ch.FileLockTable.add(Unknown Source)
	at java.base/sun.nio.ch.FileChannelImpl.lock(Unknown Source)
	at java.base/java.nio.channels.FileChannel.lock(Unknown Source)
	at net.openhft.chronicle.bytes.MappedFile.resizeRafIfTooSmall(MappedFile.java:369)
	at net.openhft.chronicle.bytes.MappedFile.acquireByteStore(MappedFile.java:307)
	at net.openhft.chronicle.bytes.MappedFile.acquireByteStore(MappedFile.java:269)
	at net.openhft.chronicle.bytes.MappedBytes.acquireNextByteStore0(MappedBytes.java:434)
	at net.openhft.chronicle.bytes.MappedBytes.readVolatileInt(MappedBytes.java:792)
	at net.openhft.chronicle.queue.impl.single.SingleChronicleQueue$StoreSupplier.headerRecovery(SingleChronicleQueue.java:1027)
	at net.openhft.chronicle.queue.impl.single.SingleChronicleQueue$StoreSupplier.acquire(SingleChronicleQueue.java:981)
	at net.openhft.chronicle.queue.impl.WireStorePool.acquire(WireStorePool.java:53)
	at net.openhft.chronicle.queue.impl.single.SingleChronicleQueue.cleanupStoreFilesWithNoData(SingleChronicleQueue.java:821)
	at net.openhft.chronicle.queue.impl.single.StoreAppender.<init>(StoreAppender.java:75)
	at net.openhft.chronicle.queue.impl.single.SingleChronicleQueue.newAppender(SingleChronicleQueue.java:422)
	at net.openhft.chronicle.core.threads.CleaningThreadLocal.initialValue(CleaningThreadLocal.java:54)
	at java.base/java.lang.ThreadLocal.setInitialValue(Unknown Source)
	at java.base/java.lang.ThreadLocal.get(Unknown Source)
	at net.openhft.chronicle.core.threads.CleaningThreadLocal.get(CleaningThreadLocal.java:59)
	at net.openhft.chronicle.queue.impl.single.SingleChronicleQueue.acquireAppender(SingleChronicleQueue.java:441)
	at org.apache.cassandra.utils.binlog.BinLog.<init>(BinLog.java:133)
	at org.apache.cassandra.utils.binlog.BinLog.<init>(BinLog.java:65)
	at org.apache.cassandra.utils.binlog.BinLog$Builder.build(BinLog.java:453)
	at org.apache.cassandra.audit.BinAuditLogger.<init>(BinAuditLogger.java:55)
	... 11 common frames omitted
```
Pod result: phase=Failed, container terminated reason=Error, exitCode=3. Node never reaches CQL.
This matches the Jira stack trace frame-for-frame (AuditLogManager.<clinit> -> FBUtilities.newAuditLogger
-> BinAuditLogger.<init> -> BinLog -> SingleChronicleQueue.cleanupStoreFilesWithNoData ->
MappedFile.resizeRafIfTooSmall -> FileChannel.lock -> OverlappingFileLockException).

Full buggy log saved: /tmp/repro-17933-buggy-full.log

## 4. A/B CONTROL (cassandra:4.0.7 — fixed)

IDENTICAL workload (`/tmp/repro-17933-fix.yaml`): same audit-logging-enabled config and the same
zero-byte `20220928-12.cq4` planted in /var/lib/cassandra/audit before start. Only the image differs.

Result on 4.0.7:
```
=== control: bug-signature search ===
OverlappingFileLockException count: 0
INFO  [main] 2026-06-12 03:00:40,735 PipelineConfigurator.java:125 - Starting listening for CQL clients on /0.0.0.0:9042 (unencrypted)...
INFO  [main] 2026-06-12 03:00:40,739 CassandraDaemon.java:782 - Startup complete
INFO  [main] 2026-06-12 03:00:32,394 StorageService.java:2806 - Node /10.244.1.50:7000 state jump to NORMAL
```
CQL responds:
```
 system.now()
--------------------------------------
 eddc2610-660a-11f1-b38b-df708973d594
(1 rows)
```
Audit dir AFTER successful startup — the planted empty .cq4 was deleted by the fix:
```
total 12
-rw-r--r-- 1 cassandra cassandra 131072 Jun 12 03:00 metadata.cq4t   # 20220928-12.cq4 (0 bytes) is GONE
```
Control pod phase=Running. Full control log saved: /tmp/repro-17933-fix-full.log

## 5. Conclusion

REPRODUCED on the buggy image (4.0.6): planting a zero-byte `.cq4` in the audit log directory with audit
logging enabled aborts node startup with `java.nio.channels.OverlappingFileLockException` (wrapped as
`ExceptionInInitializerError` from `AuditLogManager.<clinit>`), exactly as the Jira body describes.
The fixed image (4.0.7) runs the identical scenario and starts cleanly (deletes the empty .cq4, serves CQL).
Client/operator-visible: a node that previously ran will refuse to start after a crash leaves a 0-byte
chronicle file behind. tag_correction: none (classifier hint was accurate).
