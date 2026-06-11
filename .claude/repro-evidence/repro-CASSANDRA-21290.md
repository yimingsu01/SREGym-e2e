# CASSANDRA-21290 — "Implement atomic heartbeat file write"

- **Buggy version:** cassandra:4.1.11 (stock Docker Hub image)
- **Disposition:** REPRODUCED (verbatim startup-failure signature captured)
- **Topology:** single-node pod, namespace `repro-21290`, kind cluster (context kind-kind)
- **Fix versions (JIRA):** 4.1.12 (unreleased), 5.0.8, 6.0, 6.0-alpha2
- **Control image:** NONE — 4.1.12 is unreleased (4.1 Docker ceiling = 4.1.11), so no fixed-version A/B image exists. Control is WITHIN-VERSION (valid-file vs empty-file).
- **Fix commit:** https://github.com/apache/cassandra/commit/20d19c6627158415448312b50a2153310df42651  (PR apache/cassandra#4717)

## 1. Primary source (JIRA description, verbatim)
> It is possible for an empty heartbeat file to be written, potentially due to crash between file creation
> and the actual write. We can instead write to a temporary heartbeat file and then do an atomic move to
> rename it to the actual heartbeat file.
>
> Also fall back to the last modified time of the heartbeat file in case the file is not able to be parsed
> (it wont be able to be parsed if its empty)

The JSON contains NO CLI reproducer — it is a code-hardening description. The CAUSE (crash between
`create()` and `write()`) is a non-deterministic, un-stageable race. The CONSEQUENCE (an EMPTY heartbeat
file that cannot be parsed on the next startup) is deterministic and was staged directly by creating the
artifact (a 0-byte file).

## 2. Root cause — what the "heartbeat file" actually is
NOT the gossip generation file. It is the `check_data_resurrection` StartupCheck's heartbeat file,
implemented in `org.apache.cassandra.service.DataResurrectionCheck` (inner class `Heartbeat`).

From `DataResurrectionCheck.java` (cassandra-4.1.11):
- `public static final String DEFAULT_HEARTBEAT_FILE = "cassandra-heartbeat";`
- located at `DatabaseDescriptor.getLocalSystemKeyspacesDataFileLocations()[0]` →
  default `/var/lib/cassandra/data/cassandra-heartbeat`
- read path in `execute()` (BUGGY): `heartbeat = Heartbeat.deserializeFromJsonFile(heartbeatFile);`
  which on parse failure throws `StartupException(ERR_WRONG_DISK_STATE, "Failed to deserialize heartbeat file " + heartbeatFile)`.
- The fix (commit 20d19c6...) makes the WRITE atomic (`FBUtilities.serializeToJsonFileAtomic`, temp file +
  atomic rename) AND on READ catches the IOException and **falls back to the file's last-modified time**
  instead of failing startup.

Confirmed in the running 4.1.11 image's `/etc/cassandra/cassandra.yaml` (commented-out template):
```
#  check_data_resurrection:
#    enabled: false
#    heartbeat_file: /var/lib/cassandra/data/cassandra-heartbeat
```
=> The check is DISABLED by default. Only deployments that explicitly enable `check_data_resurrection`
are exposed to this bug. (Material caveat — recorded below.)

## 3. Staging — enable the check + pre-stage the empty-file artifact
The crash race itself cannot be staged from kubectl, but the empty file it would leave behind can.
A pod `command` (a) appends a `startup_checks` block enabling the check (sentinel `REPRO21290` so it is
idempotent and survives every container restart since /etc/cassandra resets on restart), and
(b) pre-creates a 0-byte `cassandra-heartbeat` BEFORE launching Cassandra:

```
sh -c 'grep -q REPRO21290 /etc/cassandra/cassandra.yaml || printf "\nstartup_checks:\n  check_data_resurrection:\n    enabled: true\n    heartbeat_file: /var/lib/cassandra/data/cassandra-heartbeat\n# REPRO21290\n" >> /etc/cassandra/cassandra.yaml; mkdir -p /var/lib/cassandra/data; : > /var/lib/cassandra/data/cassandra-heartbeat; ls -la /var/lib/cassandra/data/cassandra-heartbeat; exec docker-entrypoint.sh cassandra -f'
```
(Single stock image, no source build, no repo/tooling edits. emptyDir mounted at /var/lib/cassandra.)

NOTE on method: an earlier attempt to truncate a valid file then `kill -9 1` for an in-place restart did
NOT work — PID 1 is the Cassandra JVM directly (entrypoint `exec`s into it), and the Linux PID-namespace
init-protection drops SIGKILL sent to a namespace's PID 1 from WITHIN the same namespace (only an ancestor
namespace can deliver it). Pre-staging the empty file hits the IDENTICAL read-empty-file code path and is
cleaner, so that is the method used for the recorded result.

## 4. WITHIN-VERSION CONTROL (A/B) — valid file vs empty file, same 4.1.11 image

### Control A — ABSENT heartbeat file (check enabled, normal first boot): check CREATES it, node starts fine
Config dump confirmed the check is enabled:
```
startup_checks={check_data_resurrection={heartbeat_file=/var/lib/cassandra/data/cassandra-heartbeat, enabled=true}}
```
On this boot the file was ABSENT at check time; the enabled check tolerated the missing file, CREATED it
with valid JSON, and the node reached k8s Ready:
```
-rw-r--r-- 1 cassandra cassandra 45 Jun 11 21:32 /var/lib/cassandra/data/cassandra-heartbeat
{"last_heartbeat":"2026-06-11T21:32:40.371Z"}
```
=> absent (then auto-created, parseable) file => startup check passes => node boots. (pod Ready 1/1, restartCount 0.)

This is the stronger control: the check TOLERATES a MISSING file but CRASHES on a 0-byte one, so the
trigger is specifically the EMPTY-file state (not a missing file, and not a permission/IO problem — note
the empty file below is mode -rw-r--r-- and the error is a *deserialize/parse* failure, not an IOError on
open). Content emptiness is unambiguously the cause.

### Buggy run — EMPTY (0-byte) heartbeat file: node FAILS to start, CrashLoop / Error
Pre-staged artifact present at boot:
```
-rw-r--r-- 1 cassandra root 0 Jun 11 21:37 /var/lib/cassandra/data/cassandra-heartbeat
```
Startup check fails. VERBATIM SIGNATURE (last log line before the JVM exits, on every boot attempt):
```
ERROR [main] 2026-06-11 21:38:09,150 CassandraDaemon.java:900 - Failed to deserialize heartbeat file /var/lib/cassandra/data/cassandra-heartbeat
```
JVM then terminates; pod crash-loops:
```
$ kubectl get pod cass -n repro-21290
NAME   READY   STATUS   RESTARTS      AGE
cass   0/1     Error    3 (33s ago)   65s

container lastState.terminated.exitCode = 3
container lastState.terminated.reason   = Error
restartCount = 3  (CrashLoopBackOff -> Error)
```

## 5. Conclusion
The empty heartbeat file is isolated as the sole trigger (valid file => boots; empty file => StartupException
ERR_WRONG_DISK_STATE => exit code 3 => CrashLoop). This is precisely the failure CASSANDRA-21290 hardens:
the buggy 4.1.11 path `Heartbeat.deserializeFromJsonFile` throws on an unparseable/empty file with no
fallback; the fix writes the file atomically (temp + rename, so a crash can no longer leave it empty) and
on read falls back to last-modified time when the file cannot be parsed.

**Caveat:** `check_data_resurrection` is OFF by default, so only deployments that enabled it are exposed.
**Staging note:** the empty-file ARTIFACT was created directly; the crash race that produces it in the wild
(crash between create() and write()) is the un-stageable part and was not raced.

## 6. Verbatim signature (single most-telling line)
```
ERROR [main] CassandraDaemon.java:900 - Failed to deserialize heartbeat file /var/lib/cassandra/data/cassandra-heartbeat
```
(followed by JVM exit code 3 / pod CrashLoopBackOff)
