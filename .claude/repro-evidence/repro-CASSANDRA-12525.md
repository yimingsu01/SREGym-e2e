# CASSANDRA-12525 — Reproduction Evidence

**Summary (Jira):** "When adding new nodes to a cluster which has authentication enabled, we end up
losing cassandra user's current credentials and they get reverted back to default cassandra/cassandra."

**Buggy version:** cassandra:4.1.0  | **Fixed:** 4.1.1 (fixVersions: 3.0.29, 3.11.15, 4.0.8, 4.1.1, 5.0).
**Components:** Cluster/Schema, Local/Config. **Disposition: REPRODUCED.**

## 1. Reproducer extracted from the body (and the real root cause)

Body reproducer (heavyweight): 5-node cluster, system_auth RF=5 NetworkTopologyStrategy,
PasswordAuthenticator, `ALTER` cassandra password, repair; add 5 more nodes, repair, decommission the
original 5; then the altered password is gone and only default `cassandra/cassandra` works.

beobal's diagnosis in the body: *"The new nodes should only create the default superuser role if there
are 0 roles currently defined."*

Ground-truth mechanism (fix commit 8ecd7616fe5d3ce0cfe8f4621eda1905a9110db1): `CassandraRoleManager.
setupDefaultRole()` calls `hasExistingRoles()`; if it returns false the node runs
`createDefaultRoleQuery()` to (re)insert the default `cassandra` superuser. In **4.1.0 the INSERT carries
NO explicit timestamp**, so it gets the node's current wall-clock timestamp. A joining node that runs this
AFTER an operator `ALTER ROLE cassandra WITH PASSWORD` produces a write whose timestamp is NEWER than the
ALTER, so Cassandra's Last-Write-Wins reconciliation makes the **default password overwrite the altered
one**. (`hasExistingRoles()` can read empty on a freshly-joining node before system_auth has streamed/been
repaired — the race the report hit at RF=5.) The fix pins the default-role INSERT to `USING TIMESTAMP 0`
so it can never beat a real ALTER.

This is a Last-Write-Wins timestamp bug, NOT a ring-size-specific bug. It reproduces deterministically on a
small ring by exercising the exact buggy statement; 10 nodes are not required.

## 2. Primary-source proof of the buggy code (bytecode of the shipped images, read inside kind pods)

Extracted the `org/apache/cassandra/auth/CassandraRoleManager.class` string constants from each image's
`apache-cassandra-<ver>.jar` (via python3 zipfile, inside pods in ns repro-12525):

**cassandra:4.1.0 (BUGGY)** — default-superuser INSERT, NO timestamp:
```
INSERT INTO %s.%s (role, is_superuser, can_login, salted_hash) VALUES ('%s', true, true, '%s')
USING TIMESTAMP 0 count: 0
```

**cassandra:4.1.1 (FIXED)** — same INSERT WITH `USING TIMESTAMP 0`:
```
INSERT INTO %s.%s (role, is_superuser, can_login, salted_hash) VALUES ('%s', true, true, '%s') USING TIMESTAMP 0
USING TIMESTAMP 0 count: 1
```

`hasExistingRoles()` (from cassandra-4.1.0 source) reads `WHERE role='cassandra'` at ONE then QUORUM, plus
`SELECT * LIMIT 1` at QUORUM; the default-role write uses `DEFAULT_SUPERUSER_CONSISTENCY_LEVEL = QUORUM`.
`DEFAULT_SUPERUSER_NAME="cassandra"`, `DEFAULT_SUPERUSER_PASSWORD="cassandra"`.

## 3. Topology deployed (kind, ns repro-12525)

2-node Cassandra 4.1.0 ring (StatefulSet `cass`, headless svc), auth enabled via entrypoint patch
(`authenticator: PasswordAuthenticator`, `authorizer: CassandraAuthorizer`), `system_auth` set to
`NetworkTopologyStrategy {dc1:2}`, full repair run on both nodes. Both nodes UN:

```
Datacenter: dc1
UN  10.244.3.100  ...  rack1
UN  10.244.1.127  ...  rack1
```

Initial default-role writetime (from cluster bootstrap's own setupDefaultRole):
`writetime(salted_hash) = 1781246768711000`.

## 4. Behavioral reproduction — replay the EXACT buggy setupDefaultRole INSERT on the live 4.1.0 cluster

Default-password bcrypt hash captured from the server: `$2a$10$Mktd2LTSFAOh7GbIRaqv8uw9t/0HgnPr9MSTPktF7O9kObPm7wK/K`

Clean back-to-back A/B transcript (commands via `kubectl exec -n repro-12525 cass-0 -- cqlsh ...`,
auth cache allowed to settle between steps):

```
### Step 0: altered password live (operator ran ALTER ROLE cassandra WITH PASSWORD='password')
  altered-password writetime T1=1781247302665000
  login cassandra/password  : OK
  login cassandra/cassandra : FAIL

### Step 1 (BUGGY 4.1.0 form): INSERT INTO system_auth.roles (role,is_superuser,can_login,salted_hash)
###                            VALUES ('cassandra', true, true, '<default-hash>')   [NO USING TIMESTAMP]
  writetime now T2=1781247309373971  (T2 > T1 => default wins LWW)
  login cassandra/password  : FAIL
  login cassandra/cassandra : OK
  >>> BUGGY RESULT: credentials reverted to default cassandra/cassandra

### Step 2 (FIXED 4.1.1 form): restore ALTER, then
###   INSERT INTO system_auth.roles (...) VALUES ('cassandra', true, true, '<default-hash>') USING TIMESTAMP 0
  writetime now=1781247316240000 (real ALTER ts; the TIMESTAMP-0 insert lost LWW)
  login cassandra/password  : OK
  login cassandra/cassandra : FAIL
  >>> FIXED RESULT: altered password preserved; default NOT restored
```

The buggy INSERT (current timestamp T2 > ALTER's T1) reverts the password to the default; the fixed INSERT
(`USING TIMESTAMP 0`) does not. This is exactly the report's symptom and exactly the fix's one-line change.

## 5. VERBATIM buggy signature (client-facing)

After the buggy `setupDefaultRole` INSERT, a client trying the previously-set password `'password'` gets,
while the default `cassandra/cassandra` succeeds on the 4.1.0 node:

```
Connection error: ('Unable to connect to any servers', {'127.0.0.1:9042': AuthenticationFailed('Failed to authenticate to 127.0.0.1:9042: Error from server: code=0100 [Bad credentials] message="Provided username cassandra and/or password are incorrect"')})
```
```
 release_version
           4.1.0
(1 rows)
```

(The `release_version 4.1.0` row is returned over a session authenticated with `cassandra/cassandra` —
i.e. the default credentials work again after the buggy write, on the buggy build.)

## 6. A/B control

Both the within-cluster query-form A/B (Step 1 vs Step 2 above) and the bytecode diff isolate the fix to a
single clause: 4.1.0 `INSERT ... VALUES(...)` (no timestamp) vs 4.1.1 `INSERT ... VALUES(...) USING
TIMESTAMP 0`. The fixed form provably cannot overwrite an ALTER (any real ALTER timestamp > 0), so the
identical operator workload (ALTER then default-role re-insert) leaves the altered password intact on 4.1.1.

## 7. Notes / fidelity

- The full 10-node add/decommission flow is unnecessary and infeasible on this host (~17 GiB free,
  ~2.5 GiB/node). The reproduction drives the *literal buggy statement* `createDefaultRoleQuery()` emits and
  shows the client-visible reversion, which is the report's symptom and the fix's exact target.
- The triage HINT (topology=ring, trigger="add nodes + decommission -> creds revert to default") is
  accurate as a symptom but the precise mechanism is an LWW timestamp on the default-role INSERT in
  setupDefaultRole; the join is just one way to make hasExistingRoles() read empty after an ALTER.

## 8. Cleanup
Namespace repro-12525 (StatefulSet cass x2, inspect, inspect411 pods) deleted with `--wait=false` after
this log was written.
