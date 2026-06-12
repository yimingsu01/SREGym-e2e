# CASSANDRA-20972 — SELECT DISTINCT + range tombstone IllegalStateException (Part 3 reproduction)
- Buggy version: cassandra:5.0.5  -> fixed 5.0.6
- Shape: SINGLE-NODE CQL + nodetool flush (error bug).
- Root cause file: src/java/org/apache/cassandra/io/sstable/format/big/BigTableScanner.java (SSTableScanner.java:241; UnfilteredRowIterator not closed before hasNext/next on the DISTINCT + range-tombstone path).

## Reproducer (buggy) — exact, from the fix's DistinctReadTest
```sql
CREATE KEYSPACE IF NOT EXISTS k WITH replication={'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE k.tbl (id int, ck int, x int, PRIMARY KEY (id, ck));
DELETE FROM k.tbl USING TIMESTAMP 100 WHERE id = 1 AND ck < 10;
INSERT INTO k.tbl (id, ck, x) VALUES (1, 5, 7) USING TIMESTAMP 101;
```
-- nodetool flush k tbl
```sql
SELECT DISTINCT id FROM k.tbl WHERE token(id) > -9223372036854775808;
```

## VERBATIM BUGGY SIGNATURE (5.0.5)
ReadFailure; server log: IllegalStateException: The UnfilteredRowIterator ... must be closed before calling hasNext() or next() again  at SSTableScanner.java:241

## Control
Identical steps on fixed 5.0.6 return the row.
