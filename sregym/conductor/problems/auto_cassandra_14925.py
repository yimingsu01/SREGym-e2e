"""CASSANDRA-14925: DecimalSerializer.toString() can be used as an OOM attack.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-14925
Buggy: 3.11.9  ->  Fixed: 3.0.24, 3.11.10, 4.0-rc1, 4.0.

Reproduction summary (single node, pure CQL):
  Table t(pk int, ck int, d decimal, PRIMARY KEY(pk, ck)). Insert ONE live row carrying the
  malicious decimal 1E-2147483641 (scale = Integer.MAX_VALUE - 6 = 2147483641), then create
  3000 row tombstones in pk=1 (DELETE ... WHERE pk=1 AND ck=i for i=1..3000). The final client
  query `SELECT pk,ck ... WHERE pk=1 AND d = 1E-2147483641 ALLOW FILTERING;` exceeds the
  tombstone_warn_threshold (1000) during the scan, so ReadCommand$MetricRecording.onClose calls
  ReadCommand.toCQLString() -> appendCQLWhereClause -> RowFilter.toString() ->
  RowFilter$SimpleExpression.toString() -> column.type.getString(value) ->
  DecimalSerializer.toString() -> BigDecimal.toPlainString(), which materialises a
  ~2.1-billion-char string and OOMs the JVM. -XX:OnOutOfMemoryError=kill -9 then kills the
  process; the client sees a ReadFailure (code=1300). The fix guards DecimalSerializer.toString()
  to use BigDecimal.toString() (compact) when scale > 100 (configurable via
  -Dcassandra.decimal.maxscaleforstring).

VERBATIM BUGGY SIGNATURE (server log on cassandra:3.11.9 — the client ReadFailure is NOT the
signature; this server-side OOM stack is):
  ERROR [ReadStage-11] 2026-06-12 04:28:31,246 AbstractLocalAwareExecutorService.java:166 - Uncaught exception on thread Thread[ReadStage-11,10,main]
  java.lang.OutOfMemoryError: Java heap space
      at java.lang.AbstractStringBuilder.<init>(AbstractStringBuilder.java:68) ~[na:1.8.0_282]
      at java.lang.StringBuilder.<init>(StringBuilder.java:101) ~[na:1.8.0_282]
      at java.math.BigDecimal.getValueString(BigDecimal.java:3000) ~[na:1.8.0_282]
      at java.math.BigDecimal.toPlainString(BigDecimal.java:2984) ~[na:1.8.0_282]
      at org.apache.cassandra.serializers.DecimalSerializer.toString(DecimalSerializer.java:70) ~[apache-cassandra-3.11.9.jar:3.11.9]
      at org.apache.cassandra.serializers.DecimalSerializer.toString(DecimalSerializer.java:26) ~[apache-cassandra-3.11.9.jar:3.11.9]
      at org.apache.cassandra.db.marshal.AbstractType.getString(AbstractType.java:134) ~[apache-cassandra-3.11.9.jar:3.11.9]
      at org.apache.cassandra.db.filter.RowFilter$SimpleExpression.toString(RowFilter.java:860) ~[apache-cassandra-3.11.9.jar:3.11.9]
      at java.lang.String.valueOf(String.java:2994) ~[na:1.8.0_282]
      at java.lang.StringBuilder.append(StringBuilder.java:131) ~[na:1.8.0_282]
      at org.apache.cassandra.db.filter.RowFilter.toString(RowFilter.java:294) ~[apache-cassandra-3.11.9.jar:3.11.9]
      at java.lang.String.valueOf(String.java:2994) ~[na:1.8.0_282]
      at java.lang.StringBuilder.append(StringBuilder.java:131) ~[na:1.8.0_282]
      at org.apache.cassandra.db.SinglePartitionReadCommand.appendCQLWhereClause(SinglePartitionReadCommand.java:1134) ~[apache-cassandra-3.11.9.jar:3.11.9]
      at org.apache.cassandra.db.ReadCommand.toCQLString(ReadCommand.java:691) ~[apache-cassandra-3.11.9.jar:3.11.9]
      at org.apache.cassandra.db.ReadCommand$1MetricRecording.onClose(ReadCommand.java:574) ~[apache-cassandra-3.11.9.jar:3.11.9]
      at org.apache.cassandra.db.transform.BasePartitions.runOnClose(BasePartitions.java:70) ~[apache-cassandra-3.11.9.jar:3.11.9]
      at org.apache.cassandra.db.transform.BaseIterator.close(BaseIterator.java:86) ~[apache-cassandra-3.11.9.jar:3.11.9]
      at org.apache.cassandra.service.StorageProxy$LocalReadRunnable.runMayThrow(StorageProxy.java:1887) ~[apache-cassandra-3.11.9.jar:3.11.9]
      at org.apache.cassandra.service.StorageProxy$DroppableRunnable.run(StorageProxy.java:2652) ~[apache-cassandra-3.11.9.jar:3.11.9]
      ...
  ERROR [ReadStage-11] 2026-06-12 04:28:31,246 JVMStabilityInspector.java:94 - OutOfMemory error letting the JVM handle the error:
  java.lang.OutOfMemoryError: Java heap space
  #
  # java.lang.OutOfMemoryError: Java heap space
  # -XX:OnOutOfMemoryError="kill -9 %p"
  #   Executing /bin/sh -c "kill -9 1"...
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


# 3000 row tombstones in pk=1 are required to cross tombstone_warn_threshold (1000) so the
# tombstone metric-recording render runs during the scan (confirmed in the evidence log).
# CREATE statements use IF NOT EXISTS because the continuous reproducer pod re-runs this entire
# block in a loop; non-idempotent CREATEs would error on re-run and corrupt the crash-bug
# readiness probe's exit code (see ReproducerPodMitigationOracle semantics).
_TOMBSTONES = "\n".join(
    f"DELETE FROM repro14925.t WHERE pk=1 AND ck={i};" for i in range(1, 3001)
)

_REPRODUCER = f"""
CREATE KEYSPACE IF NOT EXISTS repro14925 WITH replication = {{'class': 'SimpleStrategy', 'replication_factor': 1}};
CREATE TABLE IF NOT EXISTS repro14925.t (pk int, ck int, d decimal, PRIMARY KEY (pk, ck));
INSERT INTO repro14925.t (pk, ck, d) VALUES (1, 0, 1E-2147483641);
{_TOMBSTONES}
SELECT pk, ck FROM repro14925.t WHERE pk = 1 AND d = 1E-2147483641 ALLOW FILTERING;
"""


class AutoCassandra14925(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "3.11.9"
    source_git_ref = "cassandra-3.11.9"
    # 3.11.9 already ships the bug (fixed in 3.11.10), so deploy the stock image instead of an
    # ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/serializers/DecimalSerializer.java"
    root_cause_description = (
        "DecimalSerializer.toString() renders a decimal column value with BigDecimal.toPlainString(), "
        "which for a value with a huge scale (e.g. 1E-2147483641, scale = Integer.MAX_VALUE - 6) "
        "materialises a ~2.1-billion-character string and exhausts the heap. This is reachable from an "
        "ordinary client query: a SELECT with the malicious decimal in the WHERE clause (ALLOW FILTERING) "
        "over a tombstone-heavy partition triggers the tombstone-warning render path "
        "ReadCommand.toCQLString() -> RowFilter.toString() -> RowFilter$SimpleExpression.toString() -> "
        "AbstractType.getString() -> DecimalSerializer.toString() -> BigDecimal.toPlainString(), causing an "
        "OutOfMemoryError that, with -XX:OnOutOfMemoryError=kill -9, kills the JVM (client sees ReadFailure "
        "code=1300). The fix guards DecimalSerializer.toString() to use BigDecimal.toString() (compact "
        "scientific form) when scale exceeds a configurable threshold (default 100, "
        "-Dcassandra.decimal.maxscaleforstring)."
    )

    reproducer = _REPRODUCER
    # Crash/error bug (server-side OOM kills the JVM), NOT a wrong-result bug, so expected_output
    # is intentionally left unset: the reproducer-pod readiness probe then checks cqlsh's exit code
    # (NotReady = bug present / DB unreachable, Ready = fixed).
    continuous_reproducer = True
