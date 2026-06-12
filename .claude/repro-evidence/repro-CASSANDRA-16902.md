# CASSANDRA-16902 Reproduction Evidence

## Bug
- **Key**: CASSANDRA-16902
- **Summary**: "A user should be able to view permissions of role they created"
- **Component**: Feature/Authorization
- **Buggy version**: cassandra:4.0.1
- **Fixed in**: 3.0.26, 3.11.12, 4.0.2, 4.1-alpha1, 4.1
- **A/B control**: cassandra:4.0.2 (buggy-patch+1, <= 4.0 ceiling of 20) — VALID fixed image
- **Disposition**: REPRODUCED

## Mechanism (from Jira body)
When a user creates a role, they should receive the DESCRIBE permission on that role by default.
In 4.0.1 this auto-grant-on-create is missing, so the creating (non-superuser) role cannot run
`LIST ALL PERMISSIONS OF '<child>'` for the role it just created.

Reproducer from body:
```
CREATE ROLE parent WITH PASSWORD = 'x' AND LOGIN = true;
GRANT CREATE ON ALL ROLES TO parent;
LOGIN parent;
CREATE ROLE child WITH PASSWORD = 'x' AND LOGIN = true;
LIST ALL PERMISSIONS OF 'child'; -- You are not authorized to view child's permissions
```

## Topology / config
- 1-node Cassandra pod in kind (context kind-kind), namespace `repro-16902`.
- Config-gated: requires auth enabled. cassandra.yaml patched at startup (via sed before entrypoint):
  - `authenticator: PasswordAuthenticator`
  - `authorizer: CassandraAuthorizer`
  (Default AllowAllAuthorizer would grant everything and mask the bug.)
- The `LOGIN parent` mid-session step was realized as a SEPARATE authenticated cqlsh invocation
  (`-u parent -p x`), since cqlsh LOGIN is interactive-only.

## Deploy verification
```
$ kubectl exec -n repro-16902 cass-buggy -- grep -E "^(authenticator|authorizer):" /etc/cassandra/cassandra.yaml
authenticator: PasswordAuthenticator
authorizer: CassandraAuthorizer

$ kubectl exec -n repro-16902 cass-buggy -- cqlsh -u cassandra -p cassandra -e "SHOW VERSION"
[cqlsh 6.0.0 | Cassandra 4.0.1 | CQL spec 3.4.5 | Native protocol v5]
$ kubectl exec -n repro-16902 cass-fixed -- cqlsh -u cassandra -p cassandra -e "SHOW VERSION"
[cqlsh 6.0.0 | Cassandra 4.0.2 | CQL spec 3.4.5 | Native protocol v5]
```

## BUGGY 4.0.1 — reproducer run

### Session A (superuser cassandra): create parent + grant CREATE ON ALL ROLES
```
$ kubectl exec -n repro-16902 cass-buggy -- cqlsh -u cassandra -p cassandra \
    -e "CREATE ROLE parent WITH PASSWORD='x' AND LOGIN=true; GRANT CREATE ON ALL ROLES TO parent;"
RC=0
```

### Session B (as parent): create child + LIST ALL PERMISSIONS OF 'child'
```
$ kubectl exec -n repro-16902 cass-buggy -- cqlsh -u parent -p x \
    -e "CREATE ROLE child WITH PASSWORD='x' AND LOGIN=true; LIST ALL PERMISSIONS OF 'child';"
<stdin>:1:Unauthorized: Error from server: code=2100 [Unauthorized] message="You are not authorized to view child's permissions"
command terminated with exit code 2
RC=2
```

### >>> VERBATIM BUGGY SIGNATURE <<<
```
<stdin>:1:Unauthorized: Error from server: code=2100 [Unauthorized] message="You are not authorized to view child's permissions"
```

### Proof the child role WAS created (failure is the LIST auth, not CREATE)
```
$ kubectl exec -n repro-16902 cass-buggy -- cqlsh -u cassandra -p cassandra -e "LIST ROLES;"

 role      | super | login | options | datacenters
-----------+-------+-------+---------+-------------
 cassandra |  True |  True |        {} |         ALL
     child | False |  True |        {} |         ALL
    parent | False |  True |        {} |         ALL

(3 rows)
```
The `child` role exists, so the `Unauthorized` error is specifically from `LIST ALL PERMISSIONS`,
confirming the missing default DESCRIBE grant on the created role.

## CONTROL: FIXED 4.0.2 — identical workload, NO misbehavior

### Session A (superuser): create parent + grant
```
$ kubectl exec -n repro-16902 cass-fixed -- cqlsh -u cassandra -p cassandra \
    -e "CREATE ROLE parent WITH PASSWORD='x' AND LOGIN=true; GRANT CREATE ON ALL ROLES TO parent;"
RC=0
```

### Session B (as parent): create child + LIST ALL PERMISSIONS OF 'child'
```
$ kubectl exec -n repro-16902 cass-fixed -- cqlsh -u parent -p x \
    -e "CREATE ROLE child WITH PASSWORD='x' AND LOGIN=true; LIST ALL PERMISSIONS OF 'child';"

 role | resource | permissions
------+----------+-------------


(0 rows)
RC=0
```
On 4.0.2 the same non-superuser `parent` can LIST permissions of the `child` it created:
RC=0, proper result table, NO Unauthorized error. (0 rows because no explicit grants on child
yet — the point is the query is AUTHORIZED, vs. the 2100 Unauthorized on 4.0.1.)

## Differential summary
| Step | 4.0.1 (buggy) | 4.0.2 (fixed) |
|------|---------------|---------------|
| parent LIST ALL PERMISSIONS OF 'child' | `code=2100 [Unauthorized] "You are not authorized to view child's permissions"` (RC=2) | returns table, `(0 rows)`, RC=0 |

Same workload, same auth config, same topology, same kind cluster — only the image version differs.
Clean reproduction with A/B control.

## Tag correction
None. Classifier hint (topology=1node, confidence=H, trigger=role creates child then LIST ALL
PERMISSIONS OF child -> 'not authorized to view child's permissions') matches the Jira body exactly.
Only nuance: bug is config-gated (requires PasswordAuthenticator + CassandraAuthorizer); without
auth enabled the default AllowAllAuthorizer would mask it.

## Environment / teardown
- Namespace created: repro-16902 (pods: cass-buggy on kind-worker2, cass-fixed on kind-worker3)
- Node-pinned to dodge ImagePullBackOff (4.0.1 on worker2/3; 4.0.2 only on worker3).
- Teardown: `kubectl delete ns repro-16902 --wait=false`
