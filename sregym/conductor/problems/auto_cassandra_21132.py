"""CASSANDRA-21132: Optionally force IndexStatusManager to use the optimized index status format.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-21132
Buggy: cassandra:5.0.6   ->   Fixed (opt-in flag added): cassandra:5.0.7

STUB: multi-node reproduction not yet encoded as a single-cluster Problem — see steps below.

Reproduction summary:
  A homogeneous 5.0.x cluster carrying a large number of keyspaces/tables/SAI indexes hits a
  startup deadlock on a FULL cold bring-down/bring-up. During convergence, Gossiper.getMinVersion()
  returns unknown (no peer RELEASE_VERSION advertised yet), so the SAI INDEX_STATUS gossip value
  falls back to the pre-5.0.3 legacy encoding (full keyspace name duplicated per index entry, plus
  literal status strings like "BUILD_SUCCEEDED" instead of numeric codes). With enough indexes the
  encoded string exceeds Short.MAX_VALUE (32767 bytes), so serializing the GossipDigestAck trips a
  bare assert in TypeSizes.sizeof. The ACK is never sent, the joining node stays DOWN, gossip never
  converges, the compressed format is never re-enabled -> deadlock (the failing node loops the error
  every ~5s; JVMStabilityInspector logs but does not halt).

Reproduced here on a 2-node ring (one send-target is enough to trip the size assert) with ~324 SAI
indexes (identifiers padded to the 48-char max to bloat the legacy payload). The compressed format
was ~17654 bytes pre-restart; the reverted legacy format measured 38655 bytes post-restart, past the
32767 limit.

IMPORTANT — the fix is OPT-IN, not automatic: CASSANDRA-21132 does NOT repair the underlying
getMinVersion() convergence race. It adds a new cassandra.yaml option
`force_optimized_index_status_format` (default: false). So a naive A/B with stock cassandra:5.0.7 and
the default config STILL reproduces the bug; the documented workaround/positive control is to set
`force_optimized_index_status_format: true` in cassandra.yaml.

Verbatim buggy signature (cass-0 system log, Thread[GossipStage:1]):
  java.lang.RuntimeException: java.lang.AssertionError
    ...
  Caused by: java.lang.AssertionError: null
    at org.apache.cassandra.db.TypeSizes.sizeof(TypeSizes.java:44)
    at org.apache.cassandra.gms.VersionedValue$VersionedValueSerializer.serializedSize(VersionedValue.java:381)
    at org.apache.cassandra.gms.EndpointStateSerializer.serializedSize(EndpointState.java:401)
    at org.apache.cassandra.gms.GossipDigestAckSerializer.serializedSize(GossipDigestAck.java:96)
    at org.apache.cassandra.gms.GossipDigestSynVerbHandler.doVerb(GossipDigestSynVerbHandler.java:110)
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra21132(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "5.0.6"
    source_git_ref = "cassandra-5.0.6"
    prebuilt_from_stock = True  # 5.0.6 already ships the bug; deploy the stock image, no ant build.

    # NOTE: the verbatim signature surfaces in TypeSizes.sizeof(TypeSizes.java:44), but that bare
    # `assert length <= Short.MAX_VALUE` is CORRECT — it is the detector/victim, not the root cause.
    # The actual cause is the legacy SAI index-status encoding selected by the IndexStatusManager
    # during the getMinVersion() convergence race; that is what the fix (the opt-in
    # force_optimized_index_status_format option) targets, per the JIRA title.
    root_cause_file = "src/java/org/apache/cassandra/index/IndexStatusManager.java"
    root_cause_description = (
        "Startup deadlock on a homogeneous 5.0.x cluster with many SAI indexes after a full cold "
        "bring-down/bring-up. During gossip convergence, Gossiper.getMinVersion() returns unknown "
        "(no peer RELEASE_VERSION advertised yet), so IndexStatusManager falls back to the pre-5.0.3 "
        "legacy INDEX_STATUS encoding — it duplicates the full keyspace name per index entry and "
        "writes status strings (\"BUILD_SUCCEEDED\") instead of numeric codes. With enough indexes "
        "the encoded value exceeds Short.MAX_VALUE (32767 bytes), so serializing the GossipDigestAck "
        "trips a bare `assert length <= Short.MAX_VALUE` in TypeSizes.sizeof (TypeSizes.java:44, hence "
        "message null). The ACK is never sent, the joining node stays DOWN, gossip never converges, "
        "the compressed format is never re-enabled, and the cluster deadlocks. The fix is OPT-IN: it "
        "adds a force_optimized_index_status_format cassandra.yaml option (default false) rather than "
        "fixing the convergence race, so stock 5.0.7 with the default config still reproduces. "
        "(Exact source path inferred from the JIRA title; the convergence helper is Gossiper.getMinVersion.)"
    )

    # STUB: this is a MULTI-NODE RING reproduction. It requires (a) two gossiping nodes — one
    # send-target is enough to trip the size assert, (b) a persistent cluster (data survives the
    # restart), and (c) a FULL cold bring-down then bring-up. A single CQL `reproducer` string CANNOT
    # express the scale-to-0 / scale-back-up gossip exchange that triggers the bug, so it is recorded
    # here as prose rather than flattened into one CQL (a flattened version compiles and registers but
    # silently does NOT reproduce the bug). The full steps from the evidence log:
    #
    #   1. Deploy a 2-replica StatefulSet of cassandra:5.0.6 with podManagementPolicy: OrderedReady
    #      and volumeClaimTemplates `data` (>=3Gi) mounted at /var/lib/cassandra so schema survives
    #      the restart. Wait for both nodes UN (`nodetool status`).
    #   2. Load a bloated SAI schema: ~20 keyspaces (RF=2) x 5 tables x 8 SAI indexes, with every
    #      identifier (keyspace/table/column/index) padded to the 48-char max so each legacy gossip
    #      entry costs ~120 bytes. Load to ~324 indexes (target ~300+ to exceed the 32767-byte assert).
    #      Index creation is slow (per-DDL schema agreement, ~18 indexes/min). Verify with
    #      `SELECT count(*) FROM system_schema.indexes;` and that `nodetool gossipinfo | grep
    #      INDEX_STATUS` shows the COMPRESSED numeric format (length << 32767).
    #   3. Full bring-down:  kubectl scale sts/cass --replicas=0  (wait for all pods gone).
    #   4. Full bring-up:    kubectl scale sts/cass --replicas=2  (OrderedReady => cass-0 first).
    #   5. When cass-1 starts and the two exchange gossip, cass-0's GossipStage throws the
    #      AssertionError above and loops it every ~5s; cass-1 stays DOWN (DN in nodetool status) and
    #      never joins. `nodetool gossipinfo | grep INDEX_STATUS` on cass-0 now shows the reverted
    #      LEGACY format (duplicated keyspace names + literal "BUILD_SUCCEEDED" strings, length 38655).
    reproducer = """
-- STUB: MULTI-NODE RING reproduction (CASSANDRA-21132) — NOT runnable as a single-cluster CQL block.
-- The trigger is a full cold bring-down/bring-up of a 2-node ring carrying ~324 SAI indexes, so the
-- gossip INDEX_STATUS value reverts to the legacy encoding and overflows Short.MAX_VALUE (32767 bytes)
-- when serializing the GossipDigestAck. Steps (see module docstring / class comment for full detail):
--
-- 1. Deploy a 2-replica StatefulSet of cassandra:5.0.6, podManagementPolicy: OrderedReady, with a
--    persistent volumeClaimTemplate (>=3Gi) mounted at /var/lib/cassandra (schema MUST survive the
--    restart). Wait for both nodes UN.
--
-- 2. Load a bloated SAI schema: ~20 keyspaces (RF=2) x 5 tables x 8 SAI indexes, all identifiers
--    padded to the 48-char max, to ~324 indexes. Representative (padded) DDL per keyspace:
--
--    CREATE KEYSPACE ks_3_________________________________________ WITH REPLICATION =
--        {'class': 'SimpleStrategy', 'replication_factor': 2};
--    USE ks_3_________________________________________;
--    CREATE TABLE tbl_3_1_______________________________________ (
--        pk int PRIMARY KEY,
--        col_3_1_1_________________________________ int,
--        col_3_1_2_________________________________ int
--    );
--    CREATE INDEX ix3_3_1_______________________________________
--        ON tbl_3_1_______________________________________ (col_3_1_1_________________________________)
--        USING 'sai';
--    -- ... repeat tables x5 and SAI indexes x8 per table, across ~20 keyspaces, to ~324 indexes total.
--
--    Verify: SELECT count(*) FROM system_schema.indexes;  -- expect ~324
--    Verify: nodetool gossipinfo | grep INDEX_STATUS       -- COMPRESSED numeric codes, length << 32767
--
-- 3. Full bring-down:  kubectl scale sts/cass --replicas=0   (wait for all pods to terminate)
-- 4. Full bring-up:    kubectl scale sts/cass --replicas=2   (OrderedReady => cass-0 starts first)
--
-- 5. When cass-1 starts and gossips with cass-0, cass-0's Thread[GossipStage:1] throws:
--      Caused by: java.lang.AssertionError: null
--        at org.apache.cassandra.db.TypeSizes.sizeof(TypeSizes.java:44)
--    looping every ~5s; cass-1 stays DOWN (DN) and never joins -> deadlock.
--    nodetool gossipinfo | grep INDEX_STATUS on cass-0 now shows the LEGACY format (length 38655).
--
-- WORKAROUND / positive control (the fix is OPT-IN): on cassandra:5.0.7 set
--   force_optimized_index_status_format: true  in cassandra.yaml, then repeat — both nodes reach UN
-- with no TypeSizes.sizeof assertion. Stock 5.0.7 with the default (false) STILL reproduces the bug.
"""
    # Intentionally NOT a continuous_reproducer: this multi-node prose cannot run as a reproducer pod,
    # so attaching a ReproducerPodMitigationOracle would silently fail to reproduce. Diagnosis-only.
    continuous_reproducer = False
