"""CASSANDRA-16577: Node waits for schema agreement on removed nodes.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16577
Buggy: 3.11.10  ->  Fixed: 3.11.11 (also 3.0.25, 4.0-rc1, 4.0).

STUB: multi-node reproduction not yet encoded as a single-cluster Problem — see steps below.

Reproduction summary (real multi-node ring; NOT expressible as one CQL string):
A 2-node ring is formed (cass1 seed + cassb). cassb is decommissioned (`nodetool decommission`)
and its pod is deleted, so it lingers in cass1's gossip with STATUS:LEFT and a now-stale SCHEMA
version. `CREATE KEYSPACE k` is then run on cass1, advancing cass1's schema while the removed
node's gossip entry keeps the OLD version. A fresh-identity node (cassc) is launched to bootstrap;
its join-time schema-agreement wait counts the removed node's stale gossip schema, never reaches
agreement, and aborts startup. (Discriminating test in the evidence log proved this fires at the
general join-time wait even with allocate_tokens_for_keyspace UNSET — the Jira reporter hit the same
defective waitForSchema via the allocate_tokens_for_keyspace -> allocateTokens path.)

Verbatim buggy signature (cassc, cassandra:3.11.10):
    WARN  [main] StorageService.java:941 - There are nodes in the cluster with a different schema
    version than us we did not merged schemas from, our version : (c527aae7-...), outstanding
    versions -> endpoints : {e84b6a60-24cf-30ca-9b58-452d92911703=[/10.244.3.105]}
    Exception (java.lang.RuntimeException) encountered during startup: Didn't receive schemas for
    all known versions within the timeout
    java.lang.RuntimeException: Didn't receive schemas for all known versions within the timeout
        at org.apache.cassandra.service.StorageService.waitForSchema(StorageService.java:947)
        at org.apache.cassandra.service.StorageService.joinTokenRing(StorageService.java:987)
        at org.apache.cassandra.service.StorageService.initServer(StorageService.java:753)
        at org.apache.cassandra.service.StorageService.initServer(StorageService.java:687)
        at org.apache.cassandra.service.CassandraDaemon.setup(CassandraDaemon.java:395)
        at org.apache.cassandra.service.CassandraDaemon.activate(CassandraDaemon.java:633)
        at org.apache.cassandra.service.CassandraDaemon.main(CassandraDaemon.java:786)

NOTE: This Problem is a STUB. The fault is a startup abort driven by cluster gossip state (a
decommissioned node lingering with a divergent schema version). It CANNOT be reproduced by a single
`reproducer` CQL string run against one cluster, so the full multi-node steps are recorded in the
`reproducer` field below and `continuous_reproducer` is left False (a looping single-pod reproducer
does not exist for this bug). Encoding it as a runnable Problem requires multi-pod orchestration
(form ring -> decommission + delete -> CREATE KEYSPACE -> bootstrap a fresh node).
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra16577(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "3.11.10"
    source_git_ref = "cassandra-3.11.10"
    # 3.11.10 already ships the bug, so deploy the stock image instead of an ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/service/StorageService.java"
    root_cause_description = (
        "A node bootstrapping into a ring aborts startup with RuntimeException 'Didn't receive "
        "schemas for all known versions within the timeout'. StorageService.waitForSchema (the "
        "join-time schema-agreement wait reached from joinTokenRing) counts the gossip-advertised "
        "schema version of a node that has already been decommissioned/removed (STATUS:LEFT). When "
        "a keyspace is created after that node leaves, the cluster's schema advances while the "
        "removed node's lingering gossip entry keeps the OLD version, so its stale version stays in "
        "the set of outstanding schema versions, agreement is never reached, and the new node never "
        "joins. The fix (3.11.11) stops waiting on schema versions from removed nodes."
    )

    # STUB reproducer — multi-node ring steps from the evidence log (NOT a single runnable CQL).
    # The proven-necessary trigger does NOT require allocate_tokens_for_keyspace (see DISCRIMINATING
    # TEST in the evidence log). The allocate_tokens_for_keyspace path noted at the end is the Jira
    # reporter's variant, which hits the same defective waitForSchema via BootStrapper.allocateTokens.
    reproducer = """
-- STUB: multi-node reproduction; cannot be flattened into one CQL block.
-- ORDERING IS LOAD-BEARING (form ring -> decommission -> delete -> create keyspace -> bootstrap).

-- 1. Form a 2-node ring on cassandra:3.11.10:
--      cass1 = seed (survives the whole time)
--      cassb = second node that joins cass1's ring
-- 2. Decommission cassb from cass1's ring (run on cassb):
--      nodetool decommission
--    Ring then shows only cass1 (UN).
-- 3. Delete the cassb pod. cassb now lingers in cass1's gossip with STATUS:LEFT and its
--    last-advertised SCHEMA version (still equal to cass1's schema at this point).
-- 4. Advance cass1's schema by creating a keyspace AFTER cassb has left (run on cass1):
CREATE KEYSPACE k WITH REPLICATION = {'class': 'SimpleStrategy', 'replication_factor': 1};
--    Now cass1 carries the NEW schema version while the removed cassb's gossip entry still
--    carries the OLD version (the divergence is the precondition).
-- 5. Launch a fresh-identity node cassc (cassandra:3.11.10, auto_bootstrap=true) to bootstrap
--    into the ring (cass1 as seed). cassc's join-time schema-agreement wait counts the removed
--    node's stale gossip schema, never reaches agreement, and aborts startup (~60s) with:
--      java.lang.RuntimeException: Didn't receive schemas for all known versions within the timeout
--        at org.apache.cassandra.service.StorageService.waitForSchema(StorageService.java:947)
--        at org.apache.cassandra.service.StorageService.joinTokenRing(StorageService.java:987)
--    cassc pod terminates: phase=Failed, exitCode=3.
--
-- JIRA-REPORTER VARIANT (same defect, different call site): append
--   allocate_tokens_for_keyspace: k
-- to cassc's cassandra.yaml. The abort then also reachable via
--   joinTokenRing:1073 -> getBootstrapTokens:177 -> BootStrapper.allocateTokens:206 -> waitForSchema.
-- On this Docker Hub 3.11.10 build allocate is NOT required: the general join-time wait
-- (joinTokenRing:987) aborts identically with allocate_tokens_for_keyspace unset.
"""

    # Crash/startup-abort bug (no wrong-result value to grep), so expected_output is intentionally
    # NOT set. continuous_reproducer is left False: the multi-node ring abort cannot be expressed as
    # a single looping reproducer pod (doing so would compile but silently never reproduce the bug).
    continuous_reproducer = False
