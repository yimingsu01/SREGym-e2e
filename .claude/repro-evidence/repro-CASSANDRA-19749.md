# CASSANDRA-19749 — ALTER USER | ROLE IF EXISTS creates a user/role if it does not exist

- **Disposition:** reproduced
- **Buggy version:** cassandra:4.1.5
- **Fixed control:** cassandra:4.1.6 (fixVersions include 4.1.6; 4.1 ceiling=11, 6<=11)
- **Topology:** 1 node (single pod). Matches hint (1node, confidence H).
- **Components:** Legacy/CQL
- **Namespace:** repro-19749 (isolated, dedicated pods). No user keyspace needed — bug lives in global `system_auth.roles`.
- **Tag correction:** none. Hint trigger ("ALTER USER IF EXISTS missing_user SUPERUSER -> creates the role") matches Jira body exactly.

## Reproducer (from Jira body)
Requires auth stack enabled (NOT default AllowAll):
- authenticator: PasswordAuthenticator
- authorizer: CassandraAuthorizer
- role_manager: CassandraRoleManager (default)

On 4.1.x the cassandra.yaml uses the FLAT string form (the map form in the Jira is 5.0+ syntax).
Enabled via pod command before `exec docker-entrypoint.sh cassandra -f`:
```
sed -ri 's/^authenticator:.*/authenticator: PasswordAuthenticator/' /etc/cassandra/cassandra.yaml
sed -ri 's/^authorizer:.*/authorizer: CassandraAuthorizer/' /etc/cassandra/cassandra.yaml
```

Then, as superuser `cassandra/cassandra`:
```
ALTER ROLE IF EXISTS repro19749_missing WITH SUPERUSER = true;
SELECT ... FROM system_auth.roles WHERE role = 'repro19749_missing';
-- and the ALTER USER form:
ALTER USER IF EXISTS repro19749_missinguser SUPERUSER;
```

## Auth-enabled verification (critical pre-check)
cassandra.yaml on BOTH pods after sed:
```
authenticator: PasswordAuthenticator
authorizer: CassandraAuthorizer
role_manager: CassandraRoleManager
```
Bare cqlsh (no creds) on buggy pod once CQL was up — REJECTED:
```
Connection error: ('Unable to connect to any servers', {'127.0.0.1:9042':
  AuthenticationFailed('Remote end requires authentication')})
```
Wrong password — REJECTED:
```
Connection error: ('Unable to connect to any servers', {'127.0.0.1:9042':
  AuthenticationFailed('Failed to authenticate to 127.0.0.1:9042: Error from server:
  code=0100 [Bad credentials] message="Provided username cassandra and/or password are incorrect"')})
```
=> Auth is genuinely enforced; system_auth.roles results are trustworthy.

## BUGGY 4.1.5 — raw output
Command: `kubectl exec -n repro-19749 cass-buggy -- cqlsh -u cassandra -p cassandra -e "<stmt>"`

Baseline (role does not exist):
```
 role | can_login | is_superuser
------+-----------+--------------
(0 rows)
```
Run reproducer (accepted, no error):
```
ALTER ROLE IF EXISTS repro19749_missing WITH SUPERUSER = true;
```
Result — PHANTOM ROLE CREATED (the bug):
```
 role               | can_login | is_superuser | member_of | salted_hash
--------------------+-----------+--------------+-----------+-------------
 repro19749_missing |      null |         True |      null |        null

(1 rows)
```
ALTER USER form — also creates phantom superuser:
```
ALTER USER IF EXISTS repro19749_missinguser SUPERUSER;
 role                   | can_login | is_superuser | salted_hash
------------------------+-----------+--------------+-------------
 repro19749_missinguser |      null |         True |        null

(1 rows)
```
**Verbatim buggy signature (single most-telling line):**
```
 repro19749_missing |      null |         True |      null |        null
```
A non-existent role becomes a superuser (is_superuser=True) with no login/password — created by a
statement that should have been a no-op under `IF EXISTS`. This is a privilege-escalation-class logic bug.

## FIXED 4.1.6 — A/B control (identical workload)
Baseline: 0 rows. After IDENTICAL `ALTER ROLE IF EXISTS repro19749_missing WITH SUPERUSER = true;`:
```
 role | can_login | is_superuser | member_of | salted_hash
------+-----------+--------------+-----------+-------------
(0 rows)
```
ALTER USER IF EXISTS form on fixed: also 0 rows. Correct no-op.

Sanity (non-IF-EXISTS form errors correctly on fixed, proving the role truly does not exist):
```
ALTER ROLE repro19749_noexist WITH SUPERUSER = true;
<stdin>:1:InvalidRequest: Error from server: code=2200 [Invalid query]
  message="Role repro19749_noexist doesn't exist"
```

## Discriminator
| | buggy 4.1.5 | fixed 4.1.6 |
|---|---|---|
| `ALTER ROLE IF EXISTS <missing> WITH SUPERUSER=true` then SELECT | **1 row (is_superuser=True)** | **0 rows** |
| `ALTER USER IF EXISTS <missing> SUPERUSER` then SELECT | **1 row (is_superuser=True)** | **0 rows** |

Identical inputs, opposite outputs. Clean reproduction.

## Tooling findings
None affecting this run. Note (not fixed, record only): the SREGym single-node pod template's
readiness/wait line uses bare `cqlsh -e "SELECT now()..."`, which fails under PasswordAuthenticator;
auth-gated bugs require `-u cassandra -p cassandra` and a retry loop (superuser appears ~10-90s after
CQL binds). Handled here in-session.

## Teardown
`kubectl delete ns repro-19749 --wait=false` (executed after writing this log).
