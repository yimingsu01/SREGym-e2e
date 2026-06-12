# CASSANDRA-16577 Reproduction Evidence Log

**Bug:** Node waits for schema agreement on removed nodes (when allocate_tokens_for_keyspace is enabled)
**Buggy version:** cassandra:3.11.10  |  **Fixed control:** cassandra:3.11.12 (fix landed in 3.11.11)
**Fix versions (Jira):** 3.0.25, 3.11.11, 4.0-rc1, 4.0
**Components:** Cluster/Gossip, Consistency/Bootstrap and Decommission
**Topology:** ring (real multi-node ring in kind, bare pods)  |  **Namespace:** repro-16577  |  **Keyspace:** k
**Date:** 2026-06-12  |  **Disposition: REPRODUCED**

## Reproducer extracted from Jira body
The Jira reproducer uses ccm: create 3-node vnode cluster on 3.11.10, decommission+remove 2 nodes,
CREATE KEYSPACE k, set 'allocate_tokens_for_keyspace: k', then re-add a node. The re-added node's
BootStrapper.allocateTokens -> waitForSchema waits for schema agreement from a REMOVED node and
crashes on startup with RuntimeException 'Didn't receive schemas for all known versions within the timeout'.

Mechanism (CASSANDRA-15158 / MigrationCoordinator): the removed node's last-advertised SCHEMA version
lingers in gossip (STATUS:LEFT). Because the keyspace was created AFTER the node left, cass1's schema
advanced while the removed node's gossip entry still carries the OLD version. A bootstrapping node's
StorageService.waitForSchema then counts that removed node's stale gossip schema in
MigrationCoordinator.outstandingVersions() -> never reaches agreement -> timeout -> startup abort.

CALL-SITE NOTE (verified empirically below, not just inferred): on this Docker Hub cassandra:3.11.10
build the abort fires at the GENERAL join-time schema wait (joinTokenRing:987 -> waitForSchema:947),
which runs regardless of allocate_tokens_for_keyspace. The Jira reporter's stack hits the SAME broken
waitForSchema via a second call site (joinTokenRing:1073 -> getBootstrapTokens:177 -> allocateTokens:206)
that the allocate config triggers. Both call sites share the one defective waitForSchema/outstandingVersions
logic that the fix (3.11.11) repairs. A DISCRIMINATING TEST (below) confirmed cassc crashes with the
identical signature even with allocate_tokens_for_keyspace UNSET, so on this build allocate is NOT the
necessary trigger; it is the necessary trigger only for the specific allocateTokens call site in the Jira.

## kind-faithful topology (advisor-guided)
- cass1: seed, survives whole time (= Jira node1).
- cassb: forms 2-node ring, then 'nodetool decommission' + pod deleted (= the removed node whose schema lingers; Jira's single outstanding /127.0.0.3).
- cassc: fresh-identity bootstrapper (= the re-added node). Crashes at the join-time schema wait. (Run once WITH allocate_tokens_for_keyspace: k matching the Jira, and once WITHOUT it as a discriminating test - both crash identically.)
ORDERING (load-bearing): form ring -> decommission cassb -> delete cassb -> CREATE KEYSPACE k -> launch cassc.

## Images loaded into kind (kind load had a multi-platform digest bug; used 'docker save | ctr import' instead)
cassandra:3.11.10 and cassandra:3.11.12 imported into kind-worker/worker2/worker3 containerd (k8s.io ns).

============================================================
## STEP 1-3: 2-node ring formed (buggy 3.11.10)
cass1 = 10.244.2.107 (seed), cassb = 10.244.3.105

## STEP 4: nodetool decommission cassb (BEFORE schema change) -> ring shows only cass1:
  UN  10.244.2.107  ... 497db425-b0d5-46e4-8025-40391f7f065e  rack1

## STEP 5: cassb pod deleted. gossipinfo on cass1 BEFORE keyspace create (cassb lingers, same schema):
  /10.244.3.105
    STATUS:123:LEFT,-1002895005410461039,1781507537265
    SCHEMA:20:e84b6a60-24cf-30ca-9b58-452d92911703
  /10.244.2.107
    STATUS:23:NORMAL,-1088751400921325571
    SCHEMA:18:e84b6a60-24cf-30ca-9b58-452d92911703

## STEP 6: CREATE KEYSPACE k on cass1 -> cass1 schema advances; removed node still on OLD schema.
nodetool describecluster:
  Schema versions:
    c527aae7-215f-3dec-84c5-596ec29eec3a: [10.244.2.107]
    UNREACHABLE: [10.244.3.105]
gossipinfo AFTER keyspace create (DIVERGENCE - this is the precondition):
  /10.244.3.105
    STATUS:123:LEFT,...
    SCHEMA:20:e84b6a60-24cf-30ca-9b58-452d92911703   <-- OLD (removed node)
  /10.244.2.107
    STATUS:23:NORMAL,...
    SCHEMA:204:c527aae7-215f-3dec-84c5-596ec29eec3a  <-- NEW (after CREATE KEYSPACE k)

## STEP 7: launch cassc (3.11.10) with 'allocate_tokens_for_keyspace: k' appended to cassandra.yaml.
Config confirmed in log: allocate_tokens_for_keyspace=k; num_tokens=256; auto_bootstrap=true; Cassandra version: 3.11.10
Pod terminated: phase=Failed reason=Error exitCode=3 (~60s into bootstrap).

============================================================
## VERBATIM BUGGY SIGNATURE (cassc log, kubectl logs cassc -n repro-16577):
------------------------------------------------------------
WARN  [main] 2026-06-12 07:14:20,771 StorageService.java:941 - There are nodes in the cluster with a different schema version than us we did not merged schemas from, our version : (c527aae7-215f-3dec-84c5-596ec29eec3a), outstanding versions -> endpoints : {e84b6a60-24cf-30ca-9b58-452d92911703=[/10.244.3.105]}
Exception (java.lang.RuntimeException) encountered during startup: Didn't receive schemas for all known versions within the timeout
java.lang.RuntimeException: Didn't receive schemas for all known versions within the timeout
	at org.apache.cassandra.service.StorageService.waitForSchema(StorageService.java:947)
	at org.apache.cassandra.service.StorageService.joinTokenRing(StorageService.java:987)
	at org.apache.cassandra.service.StorageService.initServer(StorageService.java:753)
	at org.apache.cassandra.service.StorageService.initServer(StorageService.java:687)
	at org.apache.cassandra.service.CassandraDaemon.setup(CassandraDaemon.java:395)
	at org.apache.cassandra.service.CassandraDaemon.activate(CassandraDaemon.java:633)
	at org.apache.cassandra.service.CassandraDaemon.main(CassandraDaemon.java:786)
ERROR [main] 2026-06-12 07:14:20,779 CassandraDaemon.java:803 - Exception encountered during startup
java.lang.RuntimeException: Didn't receive schemas for all known versions within the timeout
	at org.apache.cassandra.service.StorageService.waitForSchema(StorageService.java:947) ~[apache-cassandra-3.11.10.jar:3.11.10]
	at org.apache.cassandra.service.StorageService.joinTokenRing(StorageService.java:987) ~[apache-cassandra-3.11.10.jar:3.11.10]
	at org.apache.cassandra.service.StorageService.initServer(StorageService.java:753) ~[apache-cassandra-3.11.10.jar:3.11.10]
	at org.apache.cassandra.service.StorageService.initServer(StorageService.java:687) ~[apache-cassandra-3.11.10.jar:3.11.10]
	at org.apache.cassandra.service.CassandraDaemon.setup(CassandraDaemon.java:395) [apache-cassandra-3.11.10.jar:3.11.10]
	at org.apache.cassandra.service.CassandraDaemon.activate(CassandraDaemon.java:633) [apache-cassandra-3.11.10.jar:3.11.10]
	at org.apache.cassandra.service.CassandraDaemon.main(CassandraDaemon.java:786) [apache-cassandra-3.11.10.jar:3.11.10]

============================================================
## A/B CONTROL on FIXED cassandra:3.11.12 (fix landed in 3.11.11)
Ran the IDENTICAL sequence in the same namespace:
  cass1(3.11.12) seed -> cassb(3.11.12) joins -> nodetool decommission cassb -> delete cassb
  -> CREATE KEYSPACE k on cass1 -> launch cassc(3.11.12) with allocate_tokens_for_keyspace: k

### Precondition was set up IDENTICALLY (gossipinfo on control cass1 after CREATE KEYSPACE k):
  /10.244.1.132
    STATUS:24:NORMAL,...
    SCHEMA:703:d5f43817-55e2-3814-9c0f-b9f871e65437   <-- NEW (after CREATE KEYSPACE k)
  /10.244.3.108
    STATUS:643:LEFT,...
    SCHEMA:21:e84b6a60-24cf-30ca-9b58-452d92911703    <-- OLD (removed node still in gossip)
  (Note: 3.11.12 describecluster does NOT flag the removed node as a schema mismatch, unlike 3.11.10.)

### RESULT: cassc(3.11.12) JOINED CLEANLY - NO crash. Verbatim log of the SAME code path:
------------------------------------------------------------
INFO  StorageService.java:699 - Cassandra version: 3.11.12
INFO  StorageService.java:1568 - JOINING: schema complete, ready to bootstrap   <-- PASSES the join-time waitForSchema that 3.11.10 aborts at (joinTokenRing:987)
INFO  StorageService.java:1568 - JOINING: getting bootstrap token                <-- also passes the allocateTokens/waitForSchema path
INFO  StorageService.java:1568 - JOINING: Starting to bootstrap...
INFO  StorageService.java:1568 - JOINING: Finish joining ring
INFO  Server.java:159 - Starting listening for CQL clients on /0.0.0.0:9042 (unencrypted)...
(This control was run WITH allocate_tokens_for_keyspace: k. Both the general join wait and the allocate path are covered by the fix.)
------------------------------------------------------------
Final ring (nodetool status on cass1): 2 UN nodes, cassc=10.244.3.110 joined at 50% ownership. cassc pod phase=Running.
grep -c "Didn't receive schemas" over cassc(3.11.12) log = 0.

============================================================
## DISCRIMINATING TEST on buggy cassandra:3.11.10 - is allocate_tokens_for_keyspace the necessary trigger?
Re-ran the identical sequence (cass1 seed 10.244.1.136 -> cassb 10.244.3.112 joins -> decommission+delete
cassb -> CREATE KEYSPACE k) but launched cassc(3.11.10) WITHOUT the allocate config (plain pod, default
entrypoint). Config confirmed: allocate_tokens_for_keyspace=null.
RESULT: cassc CRASHED with the IDENTICAL signature and call site:
------------------------------------------------------------
WARN  [main] StorageService.java:941 - There are nodes in the cluster with a different schema version than us we did not merged schemas from, our version : (a72b0872-a0b5-30f3-a791-9199ccec5894), outstanding versions -> endpoints : {e84b6a60-24cf-30ca-9b58-452d92911703=[/10.244.3.112]}
java.lang.RuntimeException: Didn't receive schemas for all known versions within the timeout
	at org.apache.cassandra.service.StorageService.waitForSchema(StorageService.java:947) ~[apache-cassandra-3.11.10.jar:3.11.10]
	at org.apache.cassandra.service.StorageService.joinTokenRing(StorageService.java:987) ~[apache-cassandra-3.11.10.jar:3.11.10]
------------------------------------------------------------
=> On this Docker Hub 3.11.10 build the abort is the GENERAL join-time schema wait (joinTokenRing:987),
NOT gated by allocate_tokens_for_keyspace. The allocate config (Jira) hits the SAME broken waitForSchema
via a different call site (allocateTokens:206). The 3.11.12 control (run WITH allocate) passes both.

## CONCLUSION
REPRODUCED on cassandra:3.11.10. A node bootstrapping into a ring that still has a decommissioned/removed
node lingering in gossip (STATUS:LEFT) with a stale SCHEMA version aborts startup with RuntimeException
'Didn't receive schemas for all known versions within the timeout', because StorageService.waitForSchema
counts the removed node's stale gossip schema (MigrationCoordinator.outstandingVersions) and never reaches
agreement. Crash site on this build: joinTokenRing:987 -> waitForSchema:947 (the general join-time wait);
the Jira reporter hit the same defect via the allocate_tokens_for_keyspace path (allocateTokens:206). The
identical workload on the fixed cassandra:3.11.12 passes the schema wait ('JOINING: schema complete') and
joins the ring cleanly (2 UN). Matches Jira CASSANDRA-16577 ('Node waits for schema agreement on removed nodes').

## TAG CORRECTION
Classifier hint topology=ring is CORRECT. Trigger hint (allocate + re-add) is the Jira reporter's path but
is NOT the necessary trigger on this Docker Hub 3.11.10 build: the discriminating test above shows the crash
fires at the general join-time schema wait even with allocate_tokens_for_keyspace UNSET. The necessary
trigger is simply: a removed node lingering in gossip with a schema version that diverges from the cluster's
current schema (achieved by creating a keyspace AFTER the node leaves), then any new node bootstraps.
Also: only ONE removed-and-stays-removed node is mechanically required (the single outstanding version,
matching the Jira's single /127.0.0.3); ccm removed two only because it re-added node2 into its old slot.
Used a fresh-identity bootstrapper (cassc) instead of re-adding the same node.

## TOOLING / ENV FINDINGS (record only, not fixed)
1. 'kind load docker-image' failed for cassandra:3.11.10/3.11.12 with: ctr 'content digest ... not found'
   (multi-platform manifest + --all-platforms --digests path). Worked around with
   'docker save IMG | docker exec -i <node> ctr --namespace=k8s.io images import -' per worker node.
2. nodetool on the cassandra:3.11.12 image (JDK Temurin 1.8.0_332) fails EVERY call with
   "URISyntaxException: 'Malformed IPv6 address at index 7: rmi://[127.0.0.1]:7199'" (JDK 8u331+ RMI URL
   parsing regression). Worked around with: export JVM_OPTS="-Dcom.sun.jndi.rmiURLParsing=legacy" before
   nodetool. The 3.11.10 image (JDK 1.8.0_292) is unaffected. The Cassandra daemon itself is fine on both;
   only the nodetool RMI CLIENT is affected. This is purely an image/JDK quirk, unrelated to CASSANDRA-16577.
