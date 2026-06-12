# CASSANDRA-20171 — Reproduction Evidence Log

## Bug
**Summary:** Grant permission on keyspaces `system_views` and `system_virtual_schema` not possible.
**Component:** Feature/Virtual Tables
**Buggy version:** 5.0.4 (image `cassandra:5.0.4`)
**Fix versions (from Jira):** 4.0.18, 4.1.9, 5.0.5, 6.0-alpha1, 6.0
**A/B fixed-control image:** `cassandra:5.0.5` (buggy patch + 1 = 5.0.5 <= 5.0 ceiling of 8; available locally on kind-worker3)

## Classifier hint vs reality
- Hint: topology=1node, confidence=H, trigger=`GRANT SELECT ON KEYSPACE system_views TO role -> InvalidRequest 'Resource doesn't exist'`.
- **Tag correction: NONE.** Hint is accurate. 1-node is correct; the bug is purely in GRANT resource resolution (`system_schema.keyspaces` does not list the two virtual keyspaces, so `GRANT ... ON KEYSPACE <virtual>` fails the existence check). Single node, no ring needed.
- Caveat: the bug is **config-gated** — it requires `authenticator: PasswordAuthenticator` + `authorizer: CassandraAuthorizer` for GRANT to be a supported operation. The stock image ships AllowAll, so the cassandra.yaml was edited via the container command override before startup. (Without authz, GRANT fails with a different "not supported by AllowAllAuthorizer" error, which is NOT the target signature.)

## Exact reproducer (extracted from Jira body)
As the `cassandra` superuser, in one cqlsh session:
```
CREATE ROLE test WITH PASSWORD = 'test' AND LOGIN = true AND SUPERUSER = false;
GRANT SELECT PERMISSION ON KEYSPACE system TO test;            -- real keyspace, succeeds
GRANT SELECT PERMISSION ON KEYSPACE system_schema TO test;     -- real keyspace, succeeds
GRANT SELECT PERMISSION ON KEYSPACE system_views TO test;      -- virtual keyspace, FAILS on buggy
GRANT SELECT PERMISSION ON KEYSPACE system_virtual_schema TO test;  -- virtual keyspace, FAILS on buggy
LIST ALL PERMISSIONS OF test;
```

## Environment
- Existing kind cluster, context `kind-kind`, 4 nodes. Pods pinned to `kind-worker3` (has `cassandra:5.0.4` and `cassandra:5.0.5` cached locally), `imagePullPolicy: IfNotPresent`.
- Namespace created: `repro-20171` (isolated; no pre-existing namespace touched).
- Authz enabled via container command override:
  ```
  === authn/authz config ===
  authenticator: PasswordAuthenticator
  authorizer: CassandraAuthorizer
  role_manager: CassandraRoleManager
  ```

## Authz-live guard (neutralizes the AllowAll confounder)
Superuser login works and authz is active on BOTH pods:
```
 role      | super | login | options | datacenters
-----------+-------+-------+---------+-------------
 cassandra |  True |  True |        {} |         ALL

(1 rows)
```

---

## BUGGY 5.0.4 — raw output (VERBATIM)
Command:
```
kubectl exec -n repro-20171 cass -- cqlsh -u cassandra -p cassandra -e "
CREATE ROLE test WITH PASSWORD = 'test' AND LOGIN = true AND SUPERUSER = false;
GRANT SELECT PERMISSION ON KEYSPACE system TO test;
GRANT SELECT PERMISSION ON KEYSPACE system_schema TO test;
GRANT SELECT PERMISSION ON KEYSPACE system_views TO test;
GRANT SELECT PERMISSION ON KEYSPACE system_virtual_schema TO test;
LIST ALL PERMISSIONS OF test;"
```
Output (Warning/Recommendation lines filtered):
```
<stdin>:1:InvalidRequest: Error from server: code=2200 [Invalid query] message="Resource <keyspace system_views> doesn't exist"
<stdin>:1:InvalidRequest: Error from server: code=2200 [Invalid query] message="Resource <keyspace system_virtual_schema> doesn't exist"

 role | username | resource                 | permission
------+----------+--------------------------+------------
 test |     test |        <keyspace system> |     SELECT
 test |     test | <keyspace system_schema> |     SELECT

(2 rows)
```
**Result: BUG PRESENT.** Both virtual-keyspace GRANTs throw `Resource <keyspace ...> doesn't exist`; only the 2 real-keyspace grants land. This matches the Jira body exactly.

**VERBATIM SIGNATURE:**
```
InvalidRequest: Error from server: code=2200 [Invalid query] message="Resource <keyspace system_views> doesn't exist"
```
(The trailing `SyntaxException: line 1:0 no viable alternative at input ';'` is an artifact of the empty final heredoc line + trailing semicolon — unrelated to the bug.)

---

## FIXED 5.0.5 — A/B control, identical workload
Command (same as above, pod `cass-fixed`):
```
kubectl exec -n repro-20171 cass-fixed -- cqlsh -u cassandra -p cassandra -e "<identical statements>"
```
Output:
```
 role | username | resource                         | permission
------+----------+----------------------------------+------------
 test |     test |                <keyspace system> |     SELECT
 test |     test |         <keyspace system_schema> |     SELECT
 test |     test |          <keyspace system_views> |     SELECT
 test |     test | <keyspace system_virtual_schema> |     SELECT

(4 rows)
```
**Result: FIXED.** All four GRANTs succeed — `system_views` and `system_virtual_schema` are now grantable. No `InvalidRequest`. 4 rows vs the buggy 2 rows.

---

## Disposition: REPRODUCED
- Buggy 5.0.4 emits the exact `code=2200 [Invalid query] message="Resource <keyspace system_views> doesn't exist"` (and the same for `system_virtual_schema`), matching the Jira body verbatim.
- Fixed 5.0.5 runs the identical workload with all 4 GRANTs succeeding — clean A/B contrast.
- Within-version control also holds: on the buggy image, GRANT on real keyspaces (`system`, `system_schema`) succeeds while only the virtual keyspaces fail.

## Tooling findings
None. Standard public images, standard kind cluster, no SREGym tooling involved.

## Teardown
Namespace `repro-20171` deleted with `kubectl delete ns repro-20171 --wait=false`.
