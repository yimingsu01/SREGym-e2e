# CASSANDRA-18264 Reproduction Evidence

**Summary:** CustomClassLoader does not load jars, rendering triggers from JARs broken.
**Buggy version:** 4.1.0  | **Fixed control:** 4.1.1 (fixVersions: 4.1.1, 5.0-alpha1, 5.0)
**Component:** Legacy/Core | **Topology:** single node (1node) — matches classifier hint.
**Namespace:** repro-18264 (kind, context kind-kind)

## Root cause (from Jira body + verified against 4.1.0 source)
In 4.1.0 `CustomClassLoader.addClassPath` (src/java/org/apache/cassandra/triggers/CustomClassLoader.java):
- Line 38: `import static java.nio.file.Files.*;`  -> the `copy(...)` call is `java.nio.file.Files.copy(Path,Path)`.
- Line 81: `File out = FileUtils.createTempFile("cassandra-", ".jar", lib);`
  `FileUtils.createTempFile` (FileUtils.java:152) loops on `candidate.createFileIfNotExists()` and
  thus PHYSICALLY CREATES the empty destination file before returning it.
- Line 86: `copy(inputJar.toPath(), out.toPath());`  -> `Files.copy` WITHOUT `REPLACE_EXISTING`.
  Because `out` already exists, it throws `java.nio.file.FileAlreadyExistsException`, wrapped as `FSWriteError` (line 91).

Earlier versions (4.0.x/3.x) used Guava `com.google.common.io.Files` which overwrites by default, so the copy succeeded.
Fix in 4.1.1 adds `StandardCopyOption.REPLACE_EXISTING`:
`copy(inputJar.toPath(), out.toPath(), StandardCopyOption.REPLACE_EXISTING);`
(verified: https://raw.githubusercontent.com/apache/cassandra/cassandra-4.1.1/.../CustomClassLoader.java)

Effect: ANY `.jar` placed in the triggers directory makes trigger (re)loading fail. The copy fails
*before* any class is loaded, so it is not specific to a particular trigger class — it breaks the entire
trigger-from-JAR mechanism, exactly as the reporter states.

## Call chain that hits the bug
`nodetool reloadtriggers` -> `NodeProbe.reloadTriggers()` (NodeProbe.java:1919)
 -> `StorageProxy.reloadTriggerClasses()` (StorageProxy.java:2709)
 -> `TriggerExecutor.instance.reloadClasses()` (TriggerExecutor.java:64)
 -> `new CustomClassLoader(parent, triggerDirectory)` (ctor line 65)
 -> `addClassPath()` -> buggy `Files.copy` at CustomClassLoader.java:86.

## Reproducer (exact commands)

### Deploy buggy image
```
kubectl create ns repro-18264
# single-node pod, image cassandra:4.1.0 (MAX_HEAP_SIZE=1024M, GossipingPropertyFileSnitch)
kubectl wait -n repro-18264 --for=condition=Ready pod/cass --timeout=300s
# cqlsh confirms release_version = 4.1.0
```

### Trigger the bug
```
# 1) place any .jar in the triggers directory (/etc/cassandra/triggers, mode drwxrwxrwx)
kubectl exec -n repro-18264 cass -- bash -c \
  'cp $(ls /opt/cassandra/lib/*.jar | head -1) /etc/cassandra/triggers/mytrigger.jar'
# (mytrigger.jar = 78439 bytes, a valid jar; contents irrelevant — copy fails before class load)

# 2) force a trigger reload (also happens on CREATE TRIGGER / first trigger use)
kubectl exec -n repro-18264 cass -- nodetool reloadtriggers
```

## VERBATIM BUGGY OUTPUT (cassandra:4.1.0)  -- `nodetool reloadtriggers`, exit code 2
```
error: /tmp/lib/cassandra-0.jar
-- StackTrace --
java.nio.file.FileAlreadyExistsException: /tmp/lib/cassandra-0.jar
	at java.base/sun.nio.fs.UnixCopyFile.copy(Unknown Source)
	at java.base/sun.nio.fs.UnixFileSystemProvider.copy(Unknown Source)
	at java.base/java.nio.file.Files.copy(Unknown Source)
	at org.apache.cassandra.triggers.CustomClassLoader.addClassPath(CustomClassLoader.java:86)
	at org.apache.cassandra.triggers.CustomClassLoader.<init>(CustomClassLoader.java:65)
	at org.apache.cassandra.triggers.TriggerExecutor.reloadClasses(TriggerExecutor.java:64)
	at org.apache.cassandra.service.StorageProxy.reloadTriggerClasses(StorageProxy.java:2709)
	... (JMX/RMI frames)
command terminated with exit code 2
```
Server log (system.log) immediately before the failure:
```
INFO  [RMI TCP Connection(2)-127.0.0.1] 2026-06-12 05:46:50,130 CustomClassLoader.java:83 - Loading new jar /etc/cassandra/triggers/mytrigger.jar
```
(Note `cassandra-0.jar` is the temp file from createTempFile's first loop iteration num=0;
the FileAlreadyExistsException is on that just-created temp file.)

## A/B CONTROL (cassandra:4.1.1) -- IDENTICAL workload
Same namespace, pod replaced with image cassandra:4.1.1 (release_version verified = 4.1.1).
Identical steps: copy the same 78439-byte jar to /etc/cassandra/triggers/mytrigger.jar, then `nodetool reloadtriggers`.

```
=== CONTROL: nodetool reloadtriggers (4.1.1) ===
NODETOOL_EXIT=0
=== run AGAIN to exercise the overwrite/REPLACE_EXISTING path ===
NODETOOL_EXIT_2=0
=== /tmp/lib contents after two reloads ===
-rw-r--r-- 1 cassandra cassandra 78439 ... cassandra-0.jar
-rw-r--r-- 1 cassandra cassandra 78439 ... cassandra-1.jar
=== system.log ===
INFO  [RMI TCP Connection(2)-127.0.0.1] ... CustomClassLoader.java:82 - Loading new jar /etc/cassandra/triggers/mytrigger.jar
INFO  [RMI TCP Connection(4)-127.0.0.1] ... CustomClassLoader.java:82 - Loading new jar /etc/cassandra/triggers/mytrigger.jar
```

Result: exit 0 (twice). The jar is copied successfully (full 78439 bytes, not a 0-byte temp), NO
`FileAlreadyExistsException`, NO `FSWriteError`. This confirms the fix (`StandardCopyOption.REPLACE_EXISTING`).

## DISPOSITION: reproduced
- Buggy (4.1.0): `nodetool reloadtriggers` -> `java.nio.file.FileAlreadyExistsException: /tmp/lib/cassandra-0.jar`
  at `CustomClassLoader.addClassPath(CustomClassLoader.java:86)`, exit 2 — trigger loading from JAR is broken.
- Fixed (4.1.1): identical workload, exit 0, jar copied successfully.
Matches Jira body verbatim. Classifier hint (1node / load trigger from JAR -> CustomClassLoader fails to
copy JAR because Files.copy won't overwrite) is CORRECT. tag_correction: none.

## TEARDOWN
`kubectl delete ns repro-18264 --wait=false` (executed after writing this log).

