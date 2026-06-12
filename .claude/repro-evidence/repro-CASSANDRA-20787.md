# CASSANDRA-20787 — Reproduction Evidence Log

## Bug
**Summary:** Cassandra crashes on first boot with `data_disk_usage_max_disk_size` set when the data
directory is not yet created.
**Buggy version:** cassandra:5.0.4  | **Fixed-control:** cassandra:5.0.5 (fixVersions: 4.1.10, 5.0.5,
6.0-alpha1, 6.0)
**Component:** Feature/Guardrails | **Topology:** 1 node (matches classifier hint; tag_correction=none)
**Disposition:** REPRODUCED (verbatim signature + A/B control)

## Mechanism (from Jira body — ground truth)
`DatabaseDescriptor.applyGuardrails()` runs BEFORE `DatabaseDescriptor.createAllDirectories()`.
With `data_disk_usage_max_disk_size` set, `GuardrailsOptions.<init>` calls
`validateDataDiskUsageMaxDiskSize()` -> `DiskUsageMonitor.totalDiskSpace()` ->
`dataDirectoriesGroupedByFileStore()` -> `Files.getFileStore(<data dir>)`. On a fresh node the data
dir does not exist yet, so `getFileStore` throws `NoSuchFileException` and startup aborts.

## Reproducer extracted
1. Set `data_disk_usage_max_disk_size` to any value in cassandra.yaml (default is commented/null).
2. Start a FRESH node (`docker-entrypoint.sh cassandra -f`) whose data dir does not yet exist.
3. Crash on startup with the NoSuchFileException chain.

Verified preconditions in the official `cassandra:5.0.4` image:
- `data_file_directories` is commented out -> internal default used; at runtime resolves to
  `/opt/cassandra/data/data` (CASSANDRA_HOME=/opt/cassandra).
- The data dir does NOT exist in the image and the entrypoint does NOT create it (it only chowns
  existing dirs + sed-edits cassandra.yaml). So first boot satisfies the bug precondition.

## Environment
- Existing kind cluster, context kind-kind, 4 nodes. Namespace: `repro-20787` (created by me).
- Buggy image `cassandra:5.0.4` was present in local docker but Docker Hub returned HTTP 429
  (unauthenticated pull rate limit) for kind nodes. Imported directly into containerd on the kind
  nodes via `docker exec <node> ctr --namespace=k8s.io images import --snapshotter=overlayfs -`
  (kind's `kind load` wrapper failed with a multi-arch `--digests` content-digest-not-found error;
  see tooling_findings). `cassandra:5.0.5` was already present on kind-worker3 (running cass-5-0-5).
- Pods used `restartPolicy: Never` so the startup crash is a stable terminal state for log capture.

---

## CYCLE 1 — BUGGY 5.0.4  => CRASH (REPRODUCED)

Manifest: /tmp/repro-20787-buggy-v2.yaml  (image cassandra:5.0.4, restartPolicy: Never)
Command injects the guardrail then runs the stock entrypoint:
```
echo "" >> /etc/cassandra/cassandra.yaml
echo "data_disk_usage_max_disk_size: 1GiB" >> /etc/cassandra/cassandra.yaml
exec /usr/local/bin/docker-entrypoint.sh cassandra -f
```

### Injection + precondition proof (head of pod log)
```
=== injected guardrail setting ===
2285:data_disk_usage_max_disk_size: 1GiB
=== data dir existence at boot ===
ls: cannot access '/var/lib/cassandra/data': No such file or directory
drwxrwxrwt 2 cassandra cassandra 4096 Aug  4  2025 /var/lib/cassandra
```

### Pod terminal state
```
NAME   READY   STATUS   RESTARTS   AGE   NODE
cass   0/1     Error    0          22s   kind-worker3
terminated: { exitCode: 3, reason: "Error" }   # crashed within ~2s of JVM start
```

### VERBATIM BUGGY SIGNATURE (kubectl logs cass -n repro-20787)
A WARN line just before the crash also confirms the data volume is fresh:
`WARN ... DatabaseDescriptor.java:724 - Only 15.616GiB free across all data volumes...`

```
Exception (java.lang.RuntimeException) encountered during startup: Cannot get data directories grouped by file store
java.lang.RuntimeException: Cannot get data directories grouped by file store
	at org.apache.cassandra.service.disk.usage.DiskUsageMonitor.dataDirectoriesGroupedByFileStore(DiskUsageMonitor.java:202)
	at org.apache.cassandra.service.disk.usage.DiskUsageMonitor.totalDiskSpace(DiskUsageMonitor.java:209)
	at org.apache.cassandra.config.GuardrailsOptions.validateDataDiskUsageMaxDiskSize(GuardrailsOptions.java:1255)
	at org.apache.cassandra.config.GuardrailsOptions.<init>(GuardrailsOptions.java:87)
	at org.apache.cassandra.config.DatabaseDescriptor.applyGuardrails(DatabaseDescriptor.java:1113)
	at org.apache.cassandra.config.DatabaseDescriptor.applyAll(DatabaseDescriptor.java:470)
	at org.apache.cassandra.config.DatabaseDescriptor.daemonInitialization(DatabaseDescriptor.java:262)
	at org.apache.cassandra.config.DatabaseDescriptor.daemonInitialization(DatabaseDescriptor.java:246)
	at org.apache.cassandra.service.CassandraDaemon.applyConfig(CassandraDaemon.java:780)
	at org.apache.cassandra.service.CassandraDaemon.activate(CassandraDaemon.java:723)
	at org.apache.cassandra.service.CassandraDaemon.main(CassandraDaemon.java:865)
Caused by: java.nio.file.NoSuchFileException: /opt/cassandra/data/data
	at java.base/sun.nio.fs.UnixException.translateToIOException(Unknown Source)
	at java.base/sun.nio.fs.UnixException.rethrowAsIOException(Unknown Source)
	at java.base/sun.nio.fs.UnixException.rethrowAsIOException(Unknown Source)
	at java.base/sun.nio.fs.UnixFileStore.devFor(Unknown Source)
	at java.base/sun.nio.fs.UnixFileStore.<init>(Unknown Source)
	at java.base/sun.nio.fs.LinuxFileStore.<init>(Unknown Source)
	at java.base/sun.nio.fs.LinuxFileSystemProvider.getFileStore(Unknown Source)
	at java.base/sun.nio.fs.LinuxFileSystemProvider.getFileStore(Unknown Source)
	at java.base/sun.nio.fs.UnixFileSystemProvider.getFileStore(Unknown Source)
	at java.base/java.nio.file.Files.getFileStore(Unknown Source)
	at org.apache.cassandra.service.disk.usage.DiskUsageMonitor.dataDirectoriesGroupedByFileStore(DiskUsageMonitor.java:196)
	... 10 more
ERROR [main] 2026-06-12 04:25:16,518 CassandraDaemon.java:887 - Exception encountered during startup
java.lang.RuntimeException: Cannot get data directories grouped by file store
	at org.apache.cassandra.service.disk.usage.DiskUsageMonitor.dataDirectoriesGroupedByFileStore(DiskUsageMonitor.java:202)
	... (same chain repeated by CassandraDaemon error handler) ...
Caused by: java.nio.file.NoSuchFileException: /opt/cassandra/data/data
	... 10 common frames omitted
```

### Match against Jira
- Exception text: EXACT — `java.lang.RuntimeException: Cannot get data directories grouped by file store`.
- Cause: EXACT type — `java.nio.file.NoSuchFileException`. Path is `/opt/cassandra/data/data`
  (the real default data dir of this image); the Jira showed a sanitized `/path/to/data`.
- Both discriminating frames present:
  `DiskUsageMonitor.dataDirectoriesGroupedByFileStore(DiskUsageMonitor.java:202)` and
  `GuardrailsOptions.validateDataDiskUsageMaxDiskSize(GuardrailsOptions.java:1255)`.
- Full call chain `applyGuardrails -> GuardrailsOptions.<init> -> validateDataDiskUsageMaxDiskSize ->
  totalDiskSpace -> dataDirectoriesGroupedByFileStore -> Files.getFileStore -> NoSuchFileException`
  matches the Jira exactly.
- Minor line-number deltas (validateDataDiskUsageMaxDiskSize at 1255 vs Jira 786; <init> at 87 vs 83;
  etc.) are because the Jira trace was captured against a different source snapshot. Same code path.

Full buggy log saved at: /tmp/repro-20787-buggy.log

---

## CYCLE 2 — CONTROL 5.0.5 (FIXED)  => BOOTS CLEAN (A/B confirmation)

Manifest: /tmp/repro-20787-control-505.yaml  (image cassandra:5.0.5, IDENTICAL injected config,
restartPolicy: Never, pinned to kind-worker3 where the image is present).

### Identical injection + identical fresh-node precondition (head of pod log)
```
=== injected guardrail setting (CONTROL 5.0.5) ===
2285:data_disk_usage_max_disk_size: 1GiB
=== data dir existence at boot ===
ls: cannot access '/var/lib/cassandra/data': No such file or directory
drwxrwxrwt 2 cassandra cassandra 4096 Oct  2  2025 /var/lib/cassandra
```

### No startup exception
`kubectl logs cass-ctl -n repro-20787 | grep -i "Cannot get data directories|NoSuchFileException|Exception encountered during startup"`
-> (no output)

### Node comes up; cqlsh answers
```
NAME       READY   STATUS    RESTARTS   AGE    NODE
cass-ctl   1/1     Running   0          100s   kind-worker3

$ kubectl exec -n repro-20787 cass-ctl -- cqlsh -e "SELECT now() FROM system.local"
 system.now()
--------------------------------------
 0f991e50-6617-11f1-b6c3-bb72821ef156

(1 rows)
```

### Fix mechanism observed
After boot, 5.0.5 has CREATED the data dir (createAllDirectories now runs before guardrail validation):
```
$ kubectl exec -n repro-20787 cass-ctl -- ls -ld /opt/cassandra/data/data /var/lib/cassandra/data
drwxr-xr-x 7 cassandra cassandra 4096 Jun 12 04:27 /opt/cassandra/data/data
drwxr-xr-x 7 cassandra cassandra 4096 Jun 12 04:27 /var/lib/cassandra/data
```

Full control log saved at: /tmp/repro-20787-control.log

---

## Conclusion
REPRODUCED. With `data_disk_usage_max_disk_size: 1GiB` and a fresh (non-existent) data dir, the
buggy image cassandra:5.0.4 crashes on startup (exit 3) with the exact Jira signature
`RuntimeException: Cannot get data directories grouped by file store` caused by
`NoSuchFileException: /opt/cassandra/data/data`, through
`GuardrailsOptions.validateDataDiskUsageMaxDiskSize` /
`DiskUsageMonitor.dataDirectoriesGroupedByFileStore`. The fixed image cassandra:5.0.5 runs the
IDENTICAL workload under the IDENTICAL precondition, boots cleanly, serves cqlsh, and creates the
data dir before guardrail validation. Topology=1node confirmed; classifier hint accurate
(tag_correction=none).
