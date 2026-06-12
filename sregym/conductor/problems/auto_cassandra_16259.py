"""STUB: cross-version in-place-upgrade reproduction not yet encoded as a single-cluster Problem — see steps below.

CASSANDRA-16259: nodetool tablehistograms throws ArrayIndexOutOfBoundsException
after an in-place 3.11.8 -> 3.11.9 upgrade.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16259
Buggy: 3.11.9   ->   Fixed: 3.11.10
Component: Observability/Metrics

Reproduction summary (an UPGRADE scenario — NOT a single fresh node):
A table that holds BOTH an sstable written by 3.11.8 (115 cell-count histogram
bucket rows = 114 offsets + overflow) and an sstable written by 3.11.9 (119 rows =
118 + overflow, because CASSANDRA-15164 raised the CellPerPartitionCount default
from 114->118) makes TableMetrics.combineHistograms size its accumulator from the
larger array, then index the smaller sstable's bucket array out of bounds the first
time the histograms are combined. A fresh single-version 3.11.9 node CANNOT
reproduce this (verified): within one version every sstable's cell-count histogram
has the same bucket count, so the mismatch only arises across the 3.11.8->3.11.9
boundary. Triggered via `nodetool tablehistograms repro16259_ks hist_bug`.

Verbatim buggy signature (from the reproduction evidence log):
  error: 115
  -- StackTrace --
  java.lang.ArrayIndexOutOfBoundsException: 115
  	at org.apache.cassandra.metrics.TableMetrics.combineHistograms(TableMetrics.java:261)
  	at org.apache.cassandra.metrics.TableMetrics.access$000(TableMetrics.java:48)
  	at org.apache.cassandra.metrics.TableMetrics$11.getValue(TableMetrics.java:376)
  	at org.apache.cassandra.metrics.TableMetrics$11.getValue(TableMetrics.java:373)
  command terminated with exit code 2

WHY THIS IS A STUB (do not flatten into one CQL / one fixed-image sequence):
The GenericCustomBuildProblem lifecycle deploys exactly one db_version (3.11.9) and
runs the reproducer against that single deployed image. The bug needs a 3.11.8-written
115-bucket sstable to COEXIST with a 3.11.9-written 119-bucket sstable on the SAME
data directory, which requires a mid-stream in-place version swap (3.11.8 -> 3.11.9 on
the same PVC). Neither a CQL string nor the deploy->inject->reproduce lifecycle can
express that cross-version upgrade, so the full multi-phase steps are captured below
in `reproducer` and `continuous_reproducer` is left False (no working single-cluster
looping reproducer pod). See the authoritative evidence log:
.claude/repro-evidence/repro-CASSANDRA-16259.md
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra16259(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "3.11.9"
    source_git_ref = "cassandra-3.11.9"
    # 3.11.9 already ships the bug (fix is 3.11.10), so deploy the stock image
    # instead of running a ~30-min `ant jar` source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/metrics/TableMetrics.java"
    root_cause_description = (
        "nodetool tablehistograms throws ArrayIndexOutOfBoundsException after an in-place "
        "3.11.8 -> 3.11.9 upgrade. TableMetrics.combineHistograms (line 261) aggregates the "
        "per-sstable estimatedColumnCount (cells-per-partition) EstimatedHistogram: it sizes the "
        "accumulator values[] from the FIRST sstable's bucket array, then for a later sstable with "
        "FEWER buckets runs `for (i=0; i<values.length; i++) values[i] += nextBucket[i]`, indexing "
        "nextBucket[i] out of bounds. CASSANDRA-15164 (shipped in 3.11.9) raised the default "
        "CellPerPartitionCount histogram bucket count 114->118, so a 3.11.8-written sstable "
        "(115 bucket rows) and a 3.11.9-written sstable (119 bucket rows) coexisting on the same "
        "table make combineHistograms throw ArrayIndexOutOfBoundsException: 115."
    )

    # STUB: this is a cross-version in-place-upgrade reproducer. It deliberately spans
    # two Cassandra versions on ONE persistent data dir (PVC) and CANNOT be expressed as
    # a single deployed image. The full phases from the evidence log are recorded here;
    # do NOT collapse this into a single CQL block (it would compile and register but
    # silently fail to reproduce the bug).
    reproducer = """
-- ============================================================================
-- STUB / TODO: cross-version (in-place upgrade) reproduction — NOT a single CQL.
-- Requires a single Cassandra pod on a persistent 1Gi PVC mounted at
-- /var/lib/cassandra, with the pod image swapped IN PLACE on the SAME PVC so
-- data survives the version change. A fresh single-version 3.11.9 node does NOT
-- reproduce (verified): the histogram bucket-count mismatch only arises across
-- the 3.11.8 -> 3.11.9 boundary (CASSANDRA-15164 default 114->118).
-- ============================================================================

-- PHASE 1 — boot Cassandra 3.11.8 on the persistent data dir; seed an
--           OLD-format sstable with 115 cell-count histogram bucket rows.
CREATE KEYSPACE repro16259_ks WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE repro16259_ks.hist_bug (pk int, ck int, v text, PRIMARY KEY (pk, ck))
    WITH compaction = {'class':'SizeTieredCompactionStrategy','enabled':'false'};
-- shell: nodetool disableautocompaction repro16259_ks hist_bug
INSERT INTO repro16259_ks.hist_bug (pk, ck, v) VALUES (1, 0, 'old8a');
INSERT INTO repro16259_ks.hist_bug (pk, ck, v) VALUES (2, 0, 'old8b');
INSERT INTO repro16259_ks.hist_bug (pk, ck, v) VALUES (3, 0, 'old8c');
-- shell: nodetool flush repro16259_ks hist_bug    -> md-1-big-Data.db has bucket_rows=115
-- (sanity) shell: nodetool tablehistograms repro16259_ks hist_bug  -> EXIT 0, works on 3.11.8

-- PHASE 2 — UPGRADE IN PLACE to 3.11.9 on the SAME PVC (keep data):
-- shell: nodetool drain
-- shell: delete the pod but KEEP the PVC, then start cassandra:3.11.9 on the same PVC.
-- Confirm release_version is now 3.11.9, then add a NEW-format (119-bucket) sstable:
INSERT INTO repro16259_ks.hist_bug (pk, ck, v) VALUES (10, 0, 'new9');
INSERT INTO repro16259_ks.hist_bug (pk, ck, v) VALUES (11, 0, 'new9b');
-- shell: nodetool flush repro16259_ks hist_bug    -> md-2-big-Data.db has bucket_rows=119
-- Both sstables now coexist with MISMATCHED bucket counts: md-1=115, md-2=119.

-- PHASE 3 — TRIGGER the bug on the upgraded 3.11.9 node:
-- shell: nodetool tablehistograms repro16259_ks hist_bug
--   ==> error: 115
--   ==> java.lang.ArrayIndexOutOfBoundsException: 115
--         at org.apache.cassandra.metrics.TableMetrics.combineHistograms(TableMetrics.java:261)
--   (exit code 2)
"""

    # STUB: no working single-cluster looping reproducer pod (cross-version upgrade
    # cannot run inside the single-image deploy->inject->reproduce lifecycle). Left
    # False so a non-functional continuous reproducer is not falsely advertised.
    continuous_reproducer = False
    # Exception bug (ArrayIndexOutOfBoundsException), not a wrong-result bug: the `115`
    # is the out-of-bounds array index inside the stack trace, not a returned/persisted
    # value, so no expected_output is set.
