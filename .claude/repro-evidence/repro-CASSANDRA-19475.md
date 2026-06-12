# CASSANDRA-19475 — system_views.settings incorrectly handle array types

- **Disposition:** reproduced
- **Buggy version:** cassandra:4.1.4
- **Control (fixed):** cassandra:4.1.5 (A/B, identical workload)
- **Topology:** single node (one pod each), 2 pods in namespace `repro-19475`
- **Component:** Feature/Virtual Tables
- **fixVersions:** 4.1.5, 5.0-rc1, 5.0, 6.0-alpha1, 6.0

## Primary source (Jira body)
4.1+ gives:
```
cqlsh> select value from system_views.settings where name = 'data_file_directories';
 value
------------------------------
 [Ljava.lang.String;@21b4c4bb
```
should be the directory list, e.g. `[/home/fermat/dev/cassandra/.../data/data]`.

## Reproducer extracted
Query the virtual table `system_views.settings` for a config whose value is an array type
(`data_file_directories`, a `String[]`). The buggy code renders the Java array's default
`Object.toString()` instead of the directory contents. `name` is the partition key, so no
ALLOW FILTERING is needed. Virtual tables are on by default in 4.1 — NOT config-gated.

## Deploy
Two single-node pods in namespace `repro-19475`, plain template (no cassandra.yaml block):
- `cass-buggy`  image cassandra:4.1.4
- `cass-fixed`  image cassandra:4.1.5
Both reached Ready and answered `SELECT now() FROM system.local`.

## Commands + raw output

### BUGGY 4.1.4
```
$ kubectl exec -n repro-19475 cass-buggy -- cqlsh -e "SELECT release_version FROM system.local"
 release_version
-----------------
           4.1.4

$ kubectl exec -n repro-19475 cass-buggy -- cqlsh -e \
    "SELECT name, value FROM system_views.settings WHERE name = 'data_file_directories'"
 name                  | value
-----------------------+------------------------------
 data_file_directories | [Ljava.lang.String;@4cb1c088      <-- BUG: Java array toString()

(1 rows)

$ kubectl exec -n repro-19475 cass-buggy -- cqlsh -e \
    "SELECT name, value FROM system_views.settings WHERE name = 'commitlog_directory'"
 name                | value
---------------------+-------------------------------
 commitlog_directory | /opt/cassandra/data/commitlog    <-- scalar String renders fine

(1 rows)
```

### FIXED 4.1.5 (A/B control, identical query)
```
$ kubectl exec -n repro-19475 cass-fixed -- cqlsh -e "SELECT release_version FROM system.local"
 release_version
-----------------
           4.1.5

$ kubectl exec -n repro-19475 cass-fixed -- cqlsh -e \
    "SELECT name, value FROM system_views.settings WHERE name = 'data_file_directories'"
 name                  | value
-----------------------+----------------------------
 data_file_directories | [/opt/cassandra/data/data]       <-- CORRECT: actual directory list

(1 rows)

$ kubectl exec -n repro-19475 cass-fixed -- cqlsh -e \
    "SELECT name, value FROM system_views.settings WHERE name = 'commitlog_directory'"
 name                | value
---------------------+-------------------------------
 commitlog_directory | /opt/cassandra/data/commitlog

(1 rows)
```

## Analysis
- Client-visible wrong query result. On 4.1.4 the array-typed setting `data_file_directories`
  renders as `[Ljava.lang.String;@4cb1c088` (JVM array identity-hash toString). The hex hash is a
  per-run JVM identity hashcode; the stable, telling token is the `[Ljava.lang.String;@` prefix.
- On 4.1.5 the SAME query returns the real directory list `[/opt/cassandra/data/data]`.
- Array-specificity confirmed: the scalar `commitlog_directory` renders correctly on BOTH images
  (`/opt/cassandra/data/commitlog`), so the defect is specific to array-typed settings, exactly as
  the title ("incorrectly handle array types") and body state.
- The Docker image path differs from the Jira reporter's dev path (`/home/fermat/...`) — expected;
  the load-bearing fact is array toString garbage vs. a real directory string.

## Verdict
reproduced. Verbatim buggy signature: `data_file_directories | [Ljava.lang.String;@4cb1c088`.
Classifier hints (topology=1node, confidence=H, trigger) all correct → tag_correction = none.

## Teardown
`kubectl delete ns repro-19475 --wait=false` (only namespace created).
