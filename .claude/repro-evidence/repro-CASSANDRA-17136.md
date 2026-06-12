# CASSANDRA-17136 — FQL: Enabling via nodetool can trigger disk_failure_mode

- **Disposition:** reproduced
- **Buggy image:** cassandra:4.0.1 (pod `cass`)
- **Fixed control image:** cassandra:4.0.2 (pod `cass-fixed`) — fixVersion 4.0.2, <= 4.0 ceiling 20
- **Topology:** 1 node (single pod). Classifier hint topology=1node, confidence=H — CONFIRMED.
- **Namespace:** repro-17136 (kind-kind). Both pods in this one namespace.
- **Components:** Tool/fql

## Reproducer extracted from Jira body
A non-empty directory under the `--path` location that the Cassandra process user cannot
delete. Enabling FQL cleans the dir, hits `AccessDeniedException`, which is routed through the
disk_failure_policy (default `stop`) handler — stopping native transport + gossip and offlining
the node.

Jira repro (adapted to container; the JVM runs as user `cassandra`, `kubectl exec` is root):
```
mkdir /some/path/dir ; touch /some/path/dir/file ; chown -R user: dir ; chmod 700 dir
nodetool enablefullquerylog --path /some/path
```
Container adaptation (load-bearing detail): the nodetool client first validates that `--path`
itself is read/write/execute for the server user, so the PARENT (`/trap`) must be writable by
`cassandra` while the inner subdir (`/trap/dir`, root-owned, mode 555) is listable-but-not-
deletable. mode 555 (r-x, no write) is what yields the exact `AccessDeniedException: <path>/dir/file`
frame in the Jira (cassandra can list `dir` -> finds `file` -> delete of `file` is denied).

## Environment facts
- Cassandra JVM = PID 1, owner `cassandra` (`ps -o user,pid,comm -C java` -> `cassand+  1  java`).
- `kubectl exec` shell = uid 0 (root) -> root creates a trap the `cassandra` JVM cannot delete.
- Gate verified: `grep ^disk_failure_policy /etc/cassandra/cassandra.yaml` -> `disk_failure_policy: stop`.

## BUGGY (cassandra:4.0.1) — full transcript

### Baseline before trigger (node healthy)
```
$ kubectl exec -n repro-17136 cass -- cqlsh -e "SELECT now() FROM system.local"
 system.now()
--------------------------------------
 c21d9220-660a-11f1-8d2a-d18618ffda7f
(1 rows)
$ kubectl exec -n repro-17136 cass -- bash -c 'nodetool statusgossip; nodetool statusbinary'
running
running
```

### Trap setup (root)
```
$ kubectl exec -n repro-17136 cass -- bash -c 'mkdir -p /trap/dir; touch /trap/dir/file; chmod 777 /trap; chmod 555 /trap/dir; ls -la /trap; ls -la /trap/dir'
drwxrwxrwx 3 root root 4096 ... /trap
dr-xr-xr-x 2 root root 4096 ... /trap/dir
-rw-r--r-- 1 root root    0 ... /trap/dir/file
# cassandra user cannot delete:
$ su -s /bin/bash cassandra -c "rm -f /trap/dir/file; echo rm_exit=$?"
rm: cannot remove '/trap/dir/file': Permission denied
rm_exit=1
```

### Trigger -> verbatim buggy client signature
```
$ kubectl exec -n repro-17136 cass -- nodetool enablefullquerylog --path /trap
error: /trap/dir/file
-- StackTrace --
java.nio.file.AccessDeniedException: /trap/dir/file
	at java.base/sun.nio.fs.UnixException.translateToIOException(Unknown Source)
	at java.base/sun.nio.fs.UnixException.rethrowAsIOException(Unknown Source)
	at java.base/sun.nio.fs.UnixException.rethrowAsIOException(Unknown Source)
	at java.base/sun.nio.fs.UnixFileSystemProvider.implDelete(Unknown Source)
	at java.base/sun.nio.fs.AbstractFileSystemProvider.delete(Unknown Source)
	at java.base/java.nio.file.Files.delete(Unknown Source)
	at org.apache.cassandra.io.util.FileUtils.deleteWithConfirm(FileUtils.java:250)
	at org.apache.cassandra.io.util.FileUtils.deleteWithConfirm(FileUtils.java:237)
	at org.apache.cassandra.utils.binlog.BinLog.deleteRecursively(BinLog.java:492)
	at org.apache.cassandra.utils.binlog.BinLog.cleanDirectory(BinLog.java:477)
	at org.apache.cassandra.utils.binlog.BinLog$Builder.build(BinLog.java:436)
	at org.apache.cassandra.fql.FullQueryLogger.enable(FullQueryLogger.java:106)
	at org.apache.cassandra.service.StorageService.enableFullQueryLogger(StorageService.java:5915)
	...
```
(Identical frames to the Jira: BinLog.deleteRecursively -> BinLog.cleanDirectory ->
BinLog$Builder.build -> FullQueryLogger.enable -> StorageService.enableFullQueryLogger.)

### Server-side offlining (THE BUG) — from `kubectl logs -n repro-17136 cass`
```
ERROR [RMI TCP Connection(8)-127.0.0.1] 2026-06-12 03:00:11,492 DefaultFSErrorHandler.java:64 - Stopping transports as disk_failure_policy is stop
ERROR [RMI TCP Connection(8)-127.0.0.1] 2026-06-12 03:00:11,492 StorageService.java:453 - Stopping native transport
INFO  [RMI TCP Connection(8)-127.0.0.1] 2026-06-12 03:00:11,495 Server.java:171 - Stop listening for CQL clients
ERROR [RMI TCP Connection(8)-127.0.0.1] 2026-06-12 03:00:11,495 StorageService.java:458 - Stopping gossiper
WARN  [RMI TCP Connection(8)-127.0.0.1] 2026-06-12 03:00:11,495 StorageService.java:357 - Stopping gossip by operator request
INFO  [RMI TCP Connection(8)-127.0.0.1] 2026-06-12 03:00:11,495 Gossiper.java:1984 - Announcing shutdown
```
(Matches the Jira server-side excerpt line-for-line.)

### Client-visible impact after trigger (node OFFLINED)
```
$ kubectl exec -n repro-17136 cass -- cqlsh -e "SELECT now() FROM system.local"
Connection error: ('Unable to connect to any servers', {'127.0.0.1:9042': ConnectionRefusedError(111, "Tried connecting to [('127.0.0.1', 9042)]. Last error: Connection refused")})
$ kubectl exec -n repro-17136 cass -- bash -c 'nodetool statusbinary; nodetool statusgossip'
not running
not running
```
Node went from `running`/`running` + working cqlsh to `not running`/`not running` + connection
refused, purely as a result of enabling FQL. Confirms "easy way to offline a cluster".

## CONTROL (cassandra:4.0.2, the fix) — identical trap, identical command

### Baseline
```
$ kubectl exec -n repro-17136 cass-fixed -- cqlsh -e "SELECT now() FROM system.local"
 system.now()
--------------------------------------
 1a858f30-660b-11f1-abaf-d9e6fb366bb2
(1 rows)
$ grep ^disk_failure_policy /etc/cassandra/cassandra.yaml  ->  disk_failure_policy: stop
# identical trap: /trap 777, /trap/dir root-owned 555, cassandra rm -> Permission denied (rm_exit=1)
```

### Identical trigger -> NO offlining
```
$ kubectl exec -n repro-17136 cass-fixed -- nodetool enablefullquerylog --path /trap
(no error, exit 0)
$ kubectl exec -n repro-17136 cass-fixed -- cqlsh -e "SELECT now() FROM system.local"
 system.now()
--------------------------------------
 201ab290-660b-11f1-abaf-d9e6fb366bb2
(1 rows)
$ kubectl exec -n repro-17136 cass-fixed -- bash -c 'nodetool statusbinary; nodetool statusgossip'
running
running
$ kubectl logs -n repro-17136 cass-fixed | grep -E 'Stopping native transport|Stopping gossiper|Stopping transports as disk_failure_policy'
(none — only the routine startup config dump line "disk_failure_policy=stop" is present)
```
On 4.0.2 the same FQL-enable with the same undeletable subdir does NOT throw to the client and
does NOT trip disk_failure_policy: the node stays UN, native transport + gossip stay running.
Clean A/B separation.

## Conclusion
Reproduced on cassandra:4.0.1. Enabling FQL via `nodetool enablefullquerylog --path` over a
location containing a non-deletable non-empty subdirectory throws `java.nio.file.AccessDeniedException`,
which is routed to the disk_failure_policy=stop handler and OFFLINES the node (native transport +
gossip stopped; cqlsh -> ConnectionRefused). The fixed image cassandra:4.0.2 runs the identical
workload with no offlining. Tag hints (topology=1node, confidence=H, trigger) all confirmed.
```
verbatim_signature (the bug, the undesirable offlining):
ERROR [RMI TCP Connection(8)-127.0.0.1] 2026-06-12 03:00:11,492 DefaultFSErrorHandler.java:64 - Stopping transports as disk_failure_policy is stop
```
