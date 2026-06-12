# CASSANDRA-21092 — zero-copy streaming of legacy sstables AssertionError (Part 3 reproduction)
- Buggy version: cassandra:5.0.6  -> fixed 5.0.7 (control 5.0.8)
- Shape: CROSS-VERSION (generate 3.11.19 sstables, sstableloader into a 5.0.6 node). NEEDS TWO IMAGES / a second pod -> implement as a clearly-marked STUB per the skill decision tree.
- Root cause file: src/java/org/apache/cassandra/utils/BloomFilterSerializer.java (zero-copy stream serializes a pre-4.0 bloom filter in old format; fix auto-disables zero-copy for pre-4.0 bloom-filter sstables).

## Reproducer (buggy) — CROSS-VERSION, not a single CQL
1. On a cassandra:3.11.19 pod: create ks.tbl, insert ~500 rows, nodetool flush -> me-1-big-* sstables (old bloom-filter format).
2. Copy those sstable files into a cassandra:5.0.6 pod.
3. On the 5.0.6 pod: sstableloader -d <node-ip> <ks>/<tbl>  (default stream_entire_sstables=true / zero-copy).

## VERBATIM BUGGY SIGNATURE (5.0.6)
java.lang.AssertionError: Filter should not be serialized in old format
  at org.apache.cassandra.utils.BloomFilterSerializer.serialize(BloomFilterSerializer.java:52)
  at org.apache.cassandra.utils.BloomFilter.serialize(BloomFilter.java:67)
  at org.apache.cassandra.io.sstable.format.FilterComponent.save(FilterComponent.java:78)
(wrapped in CorruptSSTableException; the stream fails.)

## Control
Fixed 5.0.8: the identical sstables load successfully (500 rows, 0 AssertionErrors).
