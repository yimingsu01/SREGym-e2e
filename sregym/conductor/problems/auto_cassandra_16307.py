"""CASSANDRA-16307: GROUP BY queries with paging can return deleted data.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16307
Buggy version: 3.11.10  ->  Fixed in: 3.11.11 (also 4.0-rc1 / 4.0)
Components: Consistency/Coordination

STUB: multi-node reproduction not yet encoded as a single-cluster Problem — see steps below.

Reproduction summary (2-node ring, RF=2):
  Insert (pk=0,ck=0) and (pk=1,ck=1) at CL=ALL so both rows live on both replicas, then create
  mirror-image per-replica divergence by deleting (0,0) on cass-0 ONLY and (1,1) on cass-1 ONLY
  (the dtest's per-node executeInternal local deletes, emulated on a real ring via gossip isolation,
  with hinted handoff DISABLED so the divergence is not silently healed by hint replay). After the
  ring re-forms (2 UN), the FIRST `CONSISTENCY ALL; PAGING 1; SELECT * FROM ks16307.t GROUP BY pk;`
  (run before blocking read-repair heals the divergence) wrongly returns a tombstoned partition.
  Correct result is 0 rows: at CL=ALL both deletes win on reconciliation. The defect is specific to
  GROUP BY + paging at CL>ONE — non-paged and non-GROUP-BY variants on the SAME diverged data both
  correctly return 0 rows.

Verbatim buggy signature (cassandra:3.11.10, the spurious deleted row at CL=ALL / PAGING 1 / GROUP BY):

    Consistency level set to ALL.
    Page size: 1

     pk | ck
    ----+----
      0 |  0

    (1 rows)

    Warnings :
    Aggregation query used without partition key

(The A/B control on the fixed image cassandra:3.11.11 returns "(0 rows)" for the same query on the
same mirror divergence.)

Why this is a STUB and not a runnable single-cluster `reproducer`:
  The bug only manifests on the multi-replica reconciliation path. It requires (a) two distinct pods,
  (b) per-replica divergence created node-locally (each DELETE must land on exactly one replica, which
  the dtest does with executeInternal and which is emulated here by disabling gossip so the issuing
  node sees its peer DOWN and does not forward the mutation), (c) hinted handoff disabled so the
  tombstone does not replay on reconnect, and (d) the query captured on its FIRST run before blocking
  read-repair heals the divergence. None of this can be expressed as a single CQL string, so this is
  intentionally NOT given a runnable `reproducer`/`continuous_reproducer`; the full multi-node steps
  are recorded in the `reproducer` field below for whoever encodes the multi-pod orchestration.
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra16307(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "3.11.10"
    source_git_ref = "cassandra-3.11.10"
    # 3.11.10 already ships the bug (fix landed in 3.11.11), so deploy the stock image
    # instead of running a ~30-min ant-jar source build.
    prebuilt_from_stock = True

    # root_cause_file is grounded in the actual fix commit on the cassandra-3.11 branch
    # (7cddbd40ce6b326df533fd6d3c4131ef70b3b068), which changed DataLimits.java (the core fix)
    # and DataResolver.java (coordinator short-read protection). The evidence log itself names
    # only the behavioral locus, not a file path — see `notes`.
    root_cause_file = "src/java/org/apache/cassandra/db/filter/DataLimits.java"
    root_cause_description = (
        "A paged GROUP BY query at CL>ONE/LOCAL_ONE can return a row from a partition that has been "
        "deleted on all replicas. With a 2-node RF=2 cluster, two partitions are inserted on both "
        "nodes and then each is deleted node-locally on a different replica, so each replica sees a "
        "different partition alive but reconciliation must yield zero live partitions. With a page "
        "size of 1, GROUP BY wrongly returns one of the tombstoned partitions. The defect is in the "
        "GROUP BY paging/counting coordination path: DataLimits.GroupByAwareCounter (the "
        "hasGroupStarted state, fixed/renamed to hasUnfinishedGroup) miscounts groups across page "
        "boundaries, and the coordinator short-read protection in DataResolver.java does not correctly "
        "detect the exhausted limit. Non-paged and non-GROUP-BY queries on the same diverged data "
        "reconcile correctly to 0 rows."
    )

    # NOTE: This is the FULL multi-node reproduction recorded for a future multi-pod encoding.
    # It is NOT a runnable single-cluster CQL block, and `continuous_reproducer`/`expected_output`
    # are deliberately NOT set (doing so would deploy a looping pod that runs this string as CQL,
    # which would never reproduce the bug — the dishonest-flatten anti-pattern). Encode it only with
    # real multi-pod orchestration (two pods cass-0 and cass-1, gossip isolation, sstabledump checks).
    reproducer = """
STUB — multi-node ring (2-node StatefulSet, RF=2, hinted handoff DISABLED). Steps:

# 0. Cluster up: 2-node StatefulSet `cass`, both UN.
#    nodetool status  ->  UN cass-0, UN cass-1

# 1. Disable hinted handoff on BOTH nodes (so isolated-delete divergence is not healed by hint replay).
nodetool disablehandoff   # on cass-0
nodetool disablehandoff   # on cass-1

# 2. Create schema and insert both partitions on both replicas at CL=ALL.
CREATE KEYSPACE ks16307 WITH replication = {'class':'SimpleStrategy','replication_factor':2};
CREATE TABLE ks16307.t (pk int, ck int, PRIMARY KEY (pk, ck));
CONSISTENCY ALL;
INSERT INTO ks16307.t (pk, ck) VALUES (0, 0);
INSERT INTO ks16307.t (pk, ck) VALUES (1, 1);
SELECT * FROM ks16307.t;   -- expect 2 rows: (1,1) and (0,0) on both replicas

# 3. Create mirror-image per-replica divergence via gossip isolation.
#    Each DELETE must land LOCAL-ONLY, i.e. be issued on the node that CURRENTLY sees its peer DOWN
#    (nodetool disablegossip is asymmetric: the node that keeps gossip ON marks the silent node DOWN
#    and will NOT forward a CL=ONE mutation to it; the node that disabled its own gossip freezes its
#    view and would still forward). So:
#
#    Phase A:  on cass-0:  nodetool disablegossip      # cass-1 now sees cass-0 DN
#              on cass-1:  CONSISTENCY ONE; DELETE FROM ks16307.t WHERE pk=1 AND ck=1;   # lands on cass-1 only
#    Phase B:  on cass-0:  nodetool enablegossip       # cass-0 now sees cass-1 (still gossip-off) DN
#              on cass-0:  CONSISTENCY ONE; DELETE FROM ks16307.t WHERE pk=0 AND ck=0;   # lands on cass-0 only
#    Reform:   on cass-1:  nodetool enablegossip       # ring back to 2 UN (handoff disabled -> no hint replay)

# 4. (Optional ground-truth check) sstabledump each node — expect MIRROR divergence, delete tstamps > insert tstamps:
#    cass-0:  key "1" liveness_info (ALIVE) ; key "0" deletion_info marked_deleted (DELETED)
#    cass-1:  key "0" liveness_info (ALIVE) ; key "1" deletion_info marked_deleted (DELETED)

# 5. THE BUGGY QUERY — run on cass-0 on its FIRST execution (before blocking read-repair heals divergence):
CONSISTENCY ALL;
PAGING 1;
SELECT * FROM ks16307.t GROUP BY pk;
#    BUGGY (3.11.10): returns 1 row -> "  0 |  0" then "(1 rows)"   [a DELETED row — WRONG]
#    FIXED (3.11.11): returns "(0 rows)"   [CORRECT]
#
#    Within-version contrast on 3.11.10, SAME diverged data (isolates the defect to paged GROUP BY):
#      CONSISTENCY ALL; PAGING OFF; SELECT * FROM ks16307.t GROUP BY pk;  -> (0 rows)  CORRECT
#      CONSISTENCY ALL; PAGING OFF; SELECT * FROM ks16307.t;              -> (0 rows)  CORRECT
#      CONSISTENCY ALL; PAGING 1;   SELECT * FROM ks16307.t GROUP BY pk;  -> (1 rows) (0,0)  WRONG (the bug)
"""
