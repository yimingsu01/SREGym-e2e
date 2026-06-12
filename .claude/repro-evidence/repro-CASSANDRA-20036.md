# CASSANDRA-20036 — Reproduction Evidence Log

## Bug
**Summary:** When snapshotting, the recreate CQL does not count UDTs from reverse clustering columns.

**Mechanism (from Jira body):** `org.apache.cassandra.schema.TableMetadata#getReferencedUserTypes`
iterates `columns()` and calls `addUserTypes(c.type, types)`. When a frozen UDT is used as a clustering
key with `CLUSTERING ORDER BY (ck DESC)`, the column's type is wrapped in `ReverseType`. The UDT-detection
does not unwrap `ReverseType`, so the UDT is missed and the snapshot's recreate script (`schema.cql`) omits
the required `CREATE TYPE` statement. The recreate script is therefore non-executable in a fresh keyspace.

**Body reproducer:**
```sql
CREATE TYPE foo (a int);
CREATE TABLE tbl (pk int, ck frozen<foo>, PRIMARY KEY(pk, ck)) WITH CLUSTERING ORDER BY (ck DESC);
```

- Buggy version: **cassandra:5.0.2**
- Fixed control: **cassandra:5.0.3** (5.0.3 is literally in fixVersions: 4.0.15, 4.1.8, **5.0.3**, 6.0-alpha1, 6.0)
- Topology: **1 node** (snapshot logic is purely local) — matches classifier hint. No tag correction.
- Components: Cluster/Schema, Local/Snapshots

## Environment
- Existing kind cluster, context `kind-kind`, 4 nodes.
- Namespace created: `repro-20036`. Keyspace: `repro20036`.
- Two pods deployed in parallel: `cass-502` (5.0.2 buggy), `cass-503` (5.0.3 fixed).

```
$ kubectl exec -n repro-20036 cass-502 -- cqlsh -e "SELECT release_version FROM system.local"
 release_version
-----------------
           5.0.2
$ kubectl exec -n repro-20036 cass-503 -- cqlsh -e "SELECT release_version FROM system.local"
 release_version
-----------------
           5.0.3
```

## Setup (on both pods)
```sql
CREATE KEYSPACE repro20036 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TYPE repro20036.foo (a int);
CREATE TABLE repro20036.tbl_desc (pk int, ck frozen<foo>, PRIMARY KEY(pk, ck)) WITH CLUSTERING ORDER BY (ck DESC);  -- ReverseType
CREATE TABLE repro20036.tbl_asc  (pk int, ck frozen<foo>, PRIMARY KEY(pk, ck)) WITH CLUSTERING ORDER BY (ck ASC);   -- control: plain UDT
```
Then `nodetool flush repro20036` and `nodetool snapshot -t snapB repro20036`.

snapshot schema.cql paths:
- 5.0.2: `/var/lib/cassandra/data/repro20036/tbl_desc-d7a1bad0660b11f19973897375c31392/snapshots/snapB/schema.cql`
- 5.0.2: `/var/lib/cassandra/data/repro20036/tbl_asc-d96dc840660b11f19973897375c31392/snapshots/snapB/schema.cql`
- 5.0.3: `/var/lib/cassandra/data/repro20036/tbl_desc-ea5776b0660b11f181d27f7104e62170/snapshots/snapB/schema.cql`

## Evidence 1 — buggy snapshot schema.cql OMITS the CREATE TYPE (DESC / ReverseType)
`5.0.2`, `tbl_desc/snapshots/snapB/schema.cql` — the file jumps straight to CREATE TABLE, no CREATE TYPE:
```
CREATE TABLE IF NOT EXISTS repro20036.tbl_desc (
    pk int,
    ck frozen<foo>,
    PRIMARY KEY (pk, ck)
) WITH ID = d7a1bad0-660b-11f1-9973-897375c31392
    AND CLUSTERING ORDER BY (ck DESC)
    ...
```
```
$ grep -c 'CREATE TYPE' .../tbl_desc-.../snapshots/snapB/schema.cql
0      (exit code 1 = no match)
```

## Evidence 2 — mechanism isolation on the SAME buggy pod (ASC includes CREATE TYPE)
`5.0.2`, `tbl_asc/snapshots/snapB/schema.cql` — identical table but ASC (no ReverseType) DOES include it:
```
CREATE TYPE IF NOT EXISTS repro20036.foo (
    a int
);
CREATE TABLE IF NOT EXISTS repro20036.tbl_asc (
    ...
    AND CLUSTERING ORDER BY (ck ASC)
```
```
$ grep -c 'CREATE TYPE' .../tbl_asc-.../snapshots/snapB/schema.cql
1
```
=> Proves the omission is **ReverseType (DESC)-specific**, not "snapshots never carry UDTs."

## Evidence 3 (VERBATIM SIGNATURE) — buggy recreate script is non-executable
Drop both tables + type `foo` (keep keyspace = fresh-restore state), then replay the BUGGY snapshot schema.cql:
```
$ kubectl exec -n repro-20036 cass-502 -- cqlsh -e \
    "SELECT type_name FROM system_schema.types WHERE keyspace_name='repro20036'; \
     SELECT table_name FROM system_schema.tables WHERE keyspace_name='repro20036';"
 type_name
-----------
(0 rows)
 table_name
------------
(0 rows)

$ kubectl exec -n repro-20036 cass-502 -- bash -c \
    "cqlsh -f .../tbl_desc-d7a1bad0660b11f19973897375c31392/snapshots/snapB/schema.cql"
```
**Verbatim buggy output:**
```
.../snapshots/snapB/schema.cql:26:InvalidRequest: Error from server: code=2200 [Invalid query] message="Unknown type repro20036.foo"
command terminated with exit code 2
```
Line 26 is the `CREATE TABLE ... ck frozen<foo>` statement; the type was never defined because the bug
dropped it from the recreate script.

## Evidence 4 — A/B control on 5.0.3 (fixed): same replay SUCCEEDS
On `cass-503` (5.0.3), the DESC snapshot's schema.cql DOES contain `CREATE TYPE IF NOT EXISTS repro20036.foo`
(`grep -c 'CREATE TYPE' = 1`). Dropping the same objects (keyspace kept) and replaying it succeeds:
```
$ kubectl exec -n repro-20036 cass-503 -- bash -c "cqlsh -f .../tbl_desc-ea5776b0.../snapshots/snapB/schema.cql"
FIXED_REPLAY_EXIT=0
$ ... SELECT type_name ...        ->  foo        (1 rows)   # type recreated
$ ... SELECT table_name ...       ->  tbl_desc   (1 rows)   # table recreated
```

## Proof matrix
| Version        | Table    | Order | Column type     | CREATE TYPE in schema.cql | Replay (foo dropped) |
|----------------|----------|-------|-----------------|---------------------------|----------------------|
| 5.0.2 (buggy)  | tbl_desc | DESC  | ReverseType(foo)| **MISSING (grep=0)**      | **FAIL: Unknown type repro20036.foo** |
| 5.0.2 (buggy)  | tbl_asc  | ASC   | foo             | present (grep=1)          | (n/a control)        |
| 5.0.3 (fixed)  | tbl_desc | DESC  | ReverseType(foo)| present (grep=1)          | SUCCESS (exit 0)     |

## Bonus observation (not the primary bug)
On 5.0.2, `INSERT INTO tbl_desc (pk, ck) VALUES (1, {a: 10})` is itself rejected with
`InvalidRequest ... message="Invalid user type literal for ck of type frozen<foo>"`, while the identical
INSERT into the ASC table succeeds, and on 5.0.3 the DESC insert succeeds. This is a second client-visible
manifestation of the same ReverseType-not-unwrapped class of defect. Not pursued further; data is not
required for the snapshot reproducer (schema.cql is generated regardless).

## Secondary sub-bug NOT pursued
The body also notes UDT ordering (`CREATE TYPE a; CREATE TYPE b(b a)` emitted in hash order) can be wrong.
The body itself says it "isn't always" wrong (nondeterministic) — budget trap, deliberately skipped.

## Disposition: REPRODUCED
Deterministic. Verbatim signature:
`schema.cql:26:InvalidRequest: Error from server: code=2200 [Invalid query] message="Unknown type repro20036.foo"`

## Teardown
`kubectl delete ns repro-20036 --wait=false`
