"""CASSANDRA-21092: zero-copy streaming of legacy (pre-4.0) sstables fails with AssertionError.

STUB: multi-node reproduction not yet encoded as a single-cluster Problem — see steps below.

Title: Zero-copy streaming of legacy sstables AssertionError ("Filter should not be
serialized in old format").

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-21092
Buggy: 5.0.6  ->  Fixed: 5.0.7  (control: 5.0.8)

Shape: CROSS-VERSION. Reproducing this requires TWO database images / a second pod:
sstables are generated on an old (3.11.19) node in the legacy bloom-filter format,
then streamed via sstableloader into a 5.0.6 node with zero-copy streaming
(stream_entire_sstables=true) enabled. A single-cluster `reproducer` CQL string
CANNOT express the cross-version sstableloader step, so this is intentionally a
clearly-marked STUB rather than a flattened (and silently non-reproducing) CQL.

Reproduction (buggy, cross-version — NOT a single CQL):
  1. On a cassandra:3.11.19 pod: CREATE keyspace ks + table tbl, INSERT ~500 rows,
     then `nodetool flush` to produce me-1-big-* sstables (old bloom-filter format).
  2. Copy those sstable files into a cassandra:5.0.6 pod.
  3. On the 5.0.6 pod: `sstableloader -d <node-ip> ks/tbl`
     (default stream_entire_sstables=true / zero-copy). The stream fails.

Root cause: src/java/org/apache/cassandra/utils/BloomFilterSerializer.java — the
zero-copy stream path attempts to serialize a pre-4.0 bloom filter in the old
on-disk format and asserts. The fix (5.0.7) auto-disables zero-copy streaming for
sstables that carry a pre-4.0 (legacy) bloom filter.

Verbatim buggy signature (5.0.6):
  java.lang.AssertionError: Filter should not be serialized in old format
    at org.apache.cassandra.utils.BloomFilterSerializer.serialize(BloomFilterSerializer.java:52)
    at org.apache.cassandra.utils.BloomFilter.serialize(BloomFilter.java:67)
    at org.apache.cassandra.io.sstable.format.FilterComponent.save(FilterComponent.java:78)
  (wrapped in CorruptSSTableException; the stream fails.)

Control (fixed 5.0.8): the identical sstables load successfully (500 rows, 0 AssertionErrors).
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra21092(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "5.0.6"
    source_git_ref = "cassandra-5.0.6"
    # 5.0.6 already ships the bug (= fix patch 5.0.7 − 1), so deploy the stock image
    # instead of running a ~30-min ant-jar source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/utils/BloomFilterSerializer.java"
    root_cause_description = (
        "Zero-copy streaming (stream_entire_sstables=true) of legacy pre-4.0 sstables "
        "fails with 'java.lang.AssertionError: Filter should not be serialized in old "
        "format' at BloomFilterSerializer.serialize (wrapped in CorruptSSTableException). "
        "The zero-copy stream path tries to re-serialize a pre-4.0 bloom filter in the old "
        "on-disk format and asserts. The fix auto-disables zero-copy streaming for sstables "
        "that carry a legacy (pre-4.0) bloom-filter component."
    )

    # STUB reproducer: cross-version, multi-pod steps (NOT executable single-cluster CQL).
    # Generating legacy-format sstables on 3.11.19 and sstableloader-ing them into the
    # 5.0.6 node requires a second image / pod that this single-cluster Problem cannot
    # orchestrate. Encoded here for the agent / future single-cluster implementation.
    reproducer = """
# CROSS-VERSION reproduction — requires a cassandra:3.11.19 producer pod AND the
# buggy cassandra:5.0.6 node. This is a STUB: the steps below are NOT a single
# executable CQL block; running them as a continuous CQL reproducer would NOT
# reproduce the bug.
#
# 1. On a cassandra:3.11.19 pod, create the schema and ~500 rows of data:
#      CREATE KEYSPACE ks WITH REPLICATION = {'class': 'SimpleStrategy', 'replication_factor': 1};
#      CREATE TABLE ks.tbl (id int PRIMARY KEY, val text);
#      -- INSERT ~500 rows, e.g. INSERT INTO ks.tbl (id, val) VALUES (1, 'data'); ... (x500)
#    Then flush to disk so sstables are written in the legacy (pre-4.0) bloom-filter format:
#      nodetool flush ks tbl     # produces me-1-big-* sstable files
#
# 2. Copy the generated me-1-big-* sstable files from the 3.11.19 pod into the
#    cassandra:5.0.6 pod (e.g. via kubectl cp).
#
# 3. On the cassandra:5.0.6 pod, stream the legacy sstables with zero-copy streaming
#    (stream_entire_sstables=true is the default), which triggers the AssertionError:
#      sstableloader -d <5.0.6-node-ip> ks/tbl
#
#    Expected on 5.0.6 (buggy): the stream fails with
#      java.lang.AssertionError: Filter should not be serialized in old format
#      (wrapped in CorruptSSTableException).
#    Expected on 5.0.7+ (fixed): the 500 rows load successfully, 0 AssertionErrors.
"""

    # Stub: NOT a continuous reproducer. The reproducer string above is documented
    # multi-pod prose, not executable CQL — deploying it as a looping cqlsh workload
    # would produce a false-positive mitigation signal. Diagnosis is still graded via
    # root_cause_description; the mitigation oracle (correctly) does not attach.
    continuous_reproducer = False
    # Exception bug (AssertionError / CorruptSSTableException), not a wrong-result bug,
    # so no expected_output.
