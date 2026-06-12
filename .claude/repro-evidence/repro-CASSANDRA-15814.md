# CASSANDRA-15814 — order by descending on frozen list not working

- **Disposition:** reproduced
- **Buggy version:** cassandra:3.11.7
- **Fixed-control version:** cassandra:3.11.8 (fixVersions include 3.11.8; 8 <= 3.11 ceiling 19)
- **Topology:** single node (1node pod) — matches classifier hint
- **Components:** CQL/Interpreter
- **fixVersions (Jira):** 2.2.18, 3.0.22, 3.11.8, 4.0-beta2, 4.0
- **Namespace:** repro-15814 (keyspace `repro_15814`, underscores — hyphen illegal in keyspace name)
- **Tag correction:** none — hint matches body exactly.

## Reproducer (extracted from Jira body)
Create a table with a `frozen<list<int>>` clustering column. Without a clustering
order it accepts list-literal inserts. Adding `WITH CLUSTERING ORDER BY (version DESC)`
makes the table create normally, but an INSERT of a list literal into the clustering
column fails with `Invalid list literal ...`. The reporter's goal was to get the
latest software version via DESC ordering.

## Topology / setup
Two single-node pods deployed in parallel in namespace repro-15814:
- `cass-buggy`  image cassandra:3.11.7  (release_version 3.11.7 confirmed)
- `cass-fixed`  image cassandra:3.11.8  (release_version 3.11.8 confirmed)

Both reached Ready and answered `SELECT now() FROM system.local`.
(DDL on a fresh node needed `cqlsh --request-timeout=60`; the default ~10s client
timeout caused a transient OperationTimedOut on first attempt — NOT the bug.)

## Buggy node 3.11.7 — commands + raw output

### Schema (created OK)
```
$ kubectl exec -n repro-15814 cass-buggy -- cqlsh --request-timeout=60 -e \
  "CREATE KEYSPACE IF NOT EXISTS repro_15814 WITH replication = {'class':'SimpleStrategy','replication_factor':1};"
ks exit=0

$ ... "CREATE TABLE IF NOT EXISTS repro_15814.software_asc ( name ascii, version frozen<list<int>>, data ascii, PRIMARY KEY(name,version) );"
asc table exit=0

$ ... "CREATE TABLE IF NOT EXISTS repro_15814.software_desc ( name ascii, version frozen<list<int>>, data ascii, PRIMARY KEY(name,version) ) WITH CLUSTERING ORDER BY (version DESC);"
desc table exit=0
```
DESCRIBE confirms the DESC table:
```
CREATE TABLE repro_15814.software_desc (
    name ascii,
    version frozen<list<int>>,
    data ascii,
    PRIMARY KEY (name, version)
) WITH CLUSTERING ORDER BY (version DESC)
    ...
```

### Within-version control: ASC table insert (SUCCEEDS)
```
$ kubectl exec -n repro-15814 cass-buggy -- cqlsh --request-timeout=60 -e \
  "INSERT INTO repro_15814.software_asc(name, version) VALUES ('t1', [2,10,30,40,50]);"
asc insert exit=0

$ ... "SELECT * FROM repro_15814.software_asc;"
 name | version             | data
------+---------------------+------
   t1 | [2, 10, 30, 40, 50] | null
(1 rows)
```

### THE BUG: DESC table insert of the IDENTICAL list literal (FAILS)
```
$ kubectl exec -n repro-15814 cass-buggy -- cqlsh --request-timeout=60 -e \
  "INSERT INTO repro_15814.software_desc(name, version) VALUES ('t1', [2,10,30,40,50]);"
<stdin>:1:InvalidRequest: Error from server: code=2200 [Invalid query] message="Invalid list literal for version of type frozen<list<int>>"
command terminated with exit code 2
desc insert exit=2
```

**VERBATIM BUGGY SIGNATURE:**
```
InvalidRequest: Error from server: code=2200 [Invalid query] message="Invalid list literal for version of type frozen<list<int>>"
```
This matches the Jira body exactly. The ONLY difference between the ASC table
(insert works) and the DESC table (insert fails) is the `CLUSTERING ORDER BY
(version DESC)` clause, isolating the defect to DESC handling of a frozen-list
clustering column — not to frozen-list inserts in general.

## A/B control — fixed node 3.11.8 (identical workload SUCCEEDS)
```
$ kubectl exec -n repro-15814 cass-fixed -- cqlsh --request-timeout=60 -e \
  "CREATE KEYSPACE IF NOT EXISTS repro_15814 WITH replication = {'class':'SimpleStrategy','replication_factor':1};"
ks exit=0

$ ... "CREATE TABLE IF NOT EXISTS repro_15814.software_desc ( name ascii, version frozen<list<int>>, data ascii, PRIMARY KEY(name,version) ) WITH CLUSTERING ORDER BY (version DESC);"
desc table exit=0

$ ... "INSERT INTO repro_15814.software_desc(name, version) VALUES ('t1', [2,10,30,40,50]);"
desc insert exit=0          <-- SUCCEEDS on fixed build (failed on 3.11.7)

$ ... "INSERT ... VALUES ('t1', [1,2,3]); SELECT * FROM repro_15814.software_desc WHERE name='t1';"
 name | version             | data
------+---------------------+------
   t1 | [2, 10, 30, 40, 50] | null
   t1 |           [1, 2, 3] | null
(2 rows)
```
On 3.11.8 the DESC-clustered frozen-list table accepts the insert and returns rows
in descending clustering order, satisfying the reporter's original goal.

## Conclusion
Three data points, one variable:
1. 3.11.7 ASC table  + list-literal insert  -> SUCCESS
2. 3.11.7 DESC table + list-literal insert  -> FAIL (`Invalid list literal ...`) = the bug
3. 3.11.8 DESC table + list-literal insert  -> SUCCESS = the fix

Bug reproduced on the buggy image; A/B control on the fixed image confirms the fix.

## Tooling findings
None specific to SREGym. Operational note only: on a freshly started Cassandra pod,
schema DDL via cqlsh can exceed the default ~10s client request timeout and return
`OperationTimedOut` (a client-side timeout, distinct from the bug). Using
`cqlsh --request-timeout=60` for DDL avoids this. Not a defect in the bug under test.

## Teardown
`kubectl delete ns repro-15814 --wait=false`
