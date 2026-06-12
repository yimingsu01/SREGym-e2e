# CASSANDRA-16692 — Unable to replace node with stale schema

- **Issue:** CASSANDRA-16692 "Unable to replace node with stale schema"
- **Component:** Cluster/Schema
- **Buggy version:** cassandra:3.11.10  (fix landed in 3.11.11 / 3.0.25 / 4.0-rc1)
- **Fixed-control image:** cassandra:3.11.11 (buggy patch + 1; 3.11 ceiling = 19, so A/B is possible)
- **Topology:** ring (multi-node) — HINT confirmed correct
- **Namespace:** repro-16692 (kind, context kind-kind)
- **Disposition:** REPRODUCED

## Bug mechanism (from Jira body — ground truth)
After CASSANDRA-15158, startup waits for schema agreement across **all known endpoints, including a
down node being replaced**. CASSANDRA-16692 is the fix that exempts the replaced node. On the buggy
3.11.10 image the replacement node sees two schema versions — V0 (the dead victim, never reconcilable)
and V1 (the live seed) — and dies on a schema-agreement timeout before it can join the ring.

Body's reproducer (3 steps):
1. Shut down C* on one node in a cluster.
2. Create a new keyspace and table from one of the other nodes.
3. Terminate and replace the node on which C* was shut down → replacement startup fails.

## Exact reproducer executed (kind, standalone pods for per-pod control)
1. Deployed **seed** pod (cass:3.11.10), cluster `repro16692`. Seed IP = 10.244.2.112.
2. Deployed **victim** pod (cass:3.11.10) with `CASSANDRA_SEEDS=10.244.2.112`. Victim IP = 10.244.1.133.
   2-node ring: both `UN`, single schema version `e84b6a60-24cf-30ca-9b58-452d92911703`.
3. **Deleted** the victim pod (NOT decommission/removenode — that would purge its gossip state and the
   stale schema would vanish). Seed marked victim `DN`; victim's gossip entry persisted
   (`STATUS: shutdown,true`, `SCHEMA: e84b6a60-...` = stale V0).
4. Created keyspace+table on the **seed** → seed schema bumped to V1 `9720161e-bbcb-30a5-b369-d80903d72666`.
   Confirmed divergence via `nodetool gossipinfo`:
   - /10.244.1.133 (dead victim):  SCHEMA: e84b6a60-...  (stale V0)
   - /10.244.2.112 (live seed):    SCHEMA: 9720161e-...  (V1)
5. Deployed **replacement** pod (cass:3.11.10), `CASSANDRA_SEEDS=10.244.2.112`, fresh ephemeral data,
   `JVM_EXTRA_OPTS=-Dcassandra.replace_address_first_boot=10.244.1.133` (NOT a seed).

### Confirm replace flag took (replacement pod JVM Arguments)
```
INFO  [main] 2026-06-12 07:29:04,902 CassandraDaemon.java:507 - JVM Arguments: [... -Dcassandra.replace_address_first_boot=10.244.1.133 ...]
```
Replacement went `JOINING: waiting for ring information` (StorageService.java:1536), then timed out.

## VERBATIM BUGGY SIGNATURE (replacement pod, cassandra:3.11.10)
```
ERROR [main] 2026-06-12 07:29:53,039 CassandraDaemon.java:803 - Exception encountered during startup
java.lang.RuntimeException: Didn't receive schemas for all known versions within the timeout
	at org.apache.cassandra.service.StorageService.waitForSchema(StorageService.java:947) ~[apache-cassandra-3.11.10.jar:3.11.10]
	at org.apache.cassandra.service.StorageService.joinTokenRing(StorageService.java:987) ~[apache-cassandra-3.11.10.jar:3.11.10]
	at org.apache.cassandra.service.StorageService.initServer(StorageService.java:753) ~[apache-cassandra-3.11.10.jar:3.11.10]
	at org.apache.cassandra.service.StorageService.initServer(StorageService.java:687) ~[apache-cassandra-3.11.10.jar:3.11.10]
	at org.apache.cassandra.service.CassandraDaemon.setup(CassandraDaemon.java:395) [apache-cassandra-3.11.10.jar:3.11.10]
	at org.apache.cassandra.service.CassandraDaemon.activate(CassandraDaemon.java:633) [apache-cassandra-3.11.10.jar:3.11.10]
	at org.apache.cassandra.service.CassandraDaemon.main(CassandraDaemon.java:786) [apache-cassandra-3.11.10.jar:3.11.10]
```
Replacement pod exited (phase=Failed, container exitCode=3). Full log: /tmp/repro16692_replacement_3.11.10.log

Match to Jira: identical message + `waitForSchema` → `joinTokenRing` → `initServer` frames. Line numbers
differ only because the report was a Netflix `nf-cassandra-3.0.24.1` build; this run is stock
`apache-cassandra-3.11.10`. Same code path, same failure.

## A/B CONTROL (cassandra:3.11.11 — fixed) — PASSES (bug absent)
Identical sequence repeated on cassandra:3.11.11 (cluster `repro16692c`): seed 10.244.1.135 + victim
10.244.3.111 form 2-node ring; victim deleted (DN), gossip-stale schema `e84b6a60-...` retained; keyspace
`repro16692c_ks` created on seed (schema → `cf57c621-...`); replacement (3.11.11) deployed with
`-Dcassandra.replace_address_first_boot=10.244.3.111`.

Result on the FIXED image — NO schema-agreement timeout (0 occurrences of the buggy message). The
replacement recognized the down node and DID NOT block on it, then bootstrapped and joined:
```
WARN  [MigrationStage:1] 2026-06-12 07:34:48,832 MigrationCoordinator.java:506 - Can't send schema pull request: node /10.244.1.135 is down.
INFO  [main] 2026-06-12 07:35:00,797 StorageService.java:1564 - JOINING: schema complete, ready to bootstrap
INFO  [main] 2026-06-12 07:36:00,803 StorageService.java:1564 - JOINING: Replacing a node with token(s): [ ... ]
INFO  [main] 2026-06-12 07:36:31,035 StorageService.java:1564 - JOINING: Starting to bootstrap...
INFO  [main] 2026-06-12 07:36:31,398 StorageService.java:1621 - Bootstrap completed for tokens [ ... ]
INFO  [main] 2026-06-12 07:36:31,911 StorageService.java:2491 - Node /10.244.2.113 will complete replacement of /10.244.3.111 for tokens [ ... ]
```
Post-replacement `nodetool status` on the seed (2 UN, replacement 10.244.2.113 joined the ring):
```
UN  10.244.1.135  103.5 KiB  256          49.5%   49fa5a19-cf7d-45f6-b865-fcc206b8f266  rack1
UN  10.244.2.113  81.3 KiB   256          50.5%   579b8821-be54-4c1f-8f91-8a41efb8a0ab  rack1
```
Full fixed log: /tmp/repro16692_replacement_3.11.11_FIXED.log

## CONCLUSION
- 3.11.10 (buggy): replacement of a terminated node with a stale gossip schema FAILS at startup with
  `RuntimeException: Didn't receive schemas for all known versions within the timeout` (waitForSchema) —
  pod Failed, exit 3. MATCHES the Jira body exactly (message + frames).
- 3.11.11 (fixed): identical operation SUCCEEDS — the replaced/down node is exempted from the
  schema-agreement wait; replacement bootstraps and joins (UN).
- **Disposition: reproduced.** Classifier hints (topology=ring, trigger) were CORRECT.

## Notes
- Mechanism note: CASSANDRA-15158 introduced the all-endpoints schema wait; CASSANDRA-16692 exempts the
  replaced node. Buggy 3.11.10 has 15158 but not 16692.
- Used standalone pods (not the StatefulSet template) for per-pod replace control. Ephemeral storage only;
  empty keyspace (failure is pre-streaming) — a few rows, no cassandra-stress, well under disk budget.
- Side observation (not the canonical signature): on BOTH versions, running `CREATE KEYSPACE` on the seed
  while the peer is down returns cqlsh `NoHostAvailable` + "schema version mismatch detected" warnings; the
  DDL still applies locally (schema version bumps). This is expected DDL-vs-down-peer behavior, not the bug.

