# CASSANDRA-15857 — Frozen RawTuple is not annotated with frozen in toString -> CQLSSTableWriter throws

## Verdict: REPRODUCED (offline Java-API bug; A/B control passes)

## Bug summary (from /tmp/jira_repro/CASSANDRA-15857.json)
- Summary: "Frozen RawTuple is not annotated with frozen in the toString method"
- fixVersions: 3.11.8, 4.0-beta2, 4.0   | components: Legacy/CQL
- Mechanism: All raw CQL3 types that support freezing wrap the type with `frozen<>` in
  `toString()`, EXCEPT `RawTuple`. Since CASSANDRA-15035, a tuple nested inside a collection
  must be explicitly wrapped with `frozen`, else `RawCollection.prepare` throws. When
  `CQLSSTableWriter.Builder.build()` materializes a UDT, it round-trips the field type
  through `CQL3Type.Raw::toString` (in `Types$RawBuilder$RawUDT.prepare`) and re-parses it.
  Because `RawTuple.toString` drops the `frozen<>` wrapper, the re-parsed type becomes a
  NON-frozen tuple inside a collection -> `InvalidRequestException`.
- This is NOT a server/cqlsh-visible bug. A live server prepares UDT field types directly
  from the AST and never serializes-and-reparses them, so `CREATE TYPE` over cqlsh succeeds
  silently. The ONLY real reproducer is the offline `CQLSSTableWriter` Java API path named
  in the Jira body. No running Cassandra node is required.

## tag_correction
- topology hint "1node" is loose: ZERO running Cassandra nodes are needed. This is an offline
  bulk-loader (CQLSSTableWriter) Java-API bug; we only need the Cassandra JARs + a JVM.
- trigger hint is otherwise ACCURATE: CQLSSTableWriter on UDT `list<frozen<tuple<text,text>>>`
  -> RawTuple.toString drops frozen wrapper -> "Non-frozen tuples are not allowed" error.

## Environment / tooling notes
- Existing kind cluster (context kind-kind, 4 nodes). Namespace created: repro-15857.
- Buggy image: the candidate names 3.11.7, which was NOT cached on any kind node and Docker Hub
  pulls were rate-limited (HTTP 429). cassandra:3.11.6 WAS cached on kind-worker. 3.11.6 is the
  same pre-fix 3.11 line (CASSANDRA-15857 fixed in 3.11.8), so the bug is present identically;
  used 3.11.6 jars as the buggy substitute.
- Fixed control image: cassandra:3.11.8 (the fixVersion; 7+1=8 <= 19 ceiling), cached on kind-worker2.
- No JDK image (eclipse-temurin) was cached and pulls were rate-limited. Host docker had
  eclipse-temurin:11 (ships javac 11.0.31). `kind load docker-image` failed on a multi-arch
  manifest digest; loaded it via `docker save | docker exec <node> ctr -n k8s.io images import`
  (without --all-platforms) onto kind-worker and kind-worker2.
- Pods: a temurin:11 JDK container + an initContainer (cassandra:3.11.6 / :3.11.8) that copies
  /opt/cassandra/lib/* into a shared emptyDir; JDK container runs `sleep infinity` (no Cassandra boot).
  Compiled Repro.java with `javac -cp "/casslib/*"`; ran with `java -cp "/casslib/*:."`.
  Java 11 compiles/runs the 3.11 code (3.11 supports JDK 8 and 11); only a deprecation note
  (Config.setClientMode) was emitted at compile time.

## Reproducer program (/tmp/Repro.java — IDENTICAL for buggy and fixed)
```java
import org.apache.cassandra.config.Config;
import org.apache.cassandra.io.sstable.CQLSSTableWriter;
import java.io.File;

public class Repro {
    public static void main(String[] args) throws Exception {
        Config.setClientMode(true);
        File dir = new File("/tmp/sst");
        dir.mkdirs();
        String createType = "CREATE TYPE ks.footype ( f list<frozen<tuple<text, text>>> )";
        String createTable = "CREATE TABLE ks.t ( id int PRIMARY KEY, v frozen<footype> )";
        String insert = "INSERT INTO ks.t (id, v) VALUES (?, ?)";
        System.out.println("TYPE STMT: " + createType);
        try {
            CQLSSTableWriter writer = CQLSSTableWriter.builder()
                .inDirectory(dir).forTable(createTable).withType(createType)
                .using(insert).build();
            System.out.println("BUILD_OK: CQLSSTableWriter constructed without exception");
            writer.close();
        } catch (Throwable t) {
            System.out.println("BUILD_FAILED with: " + t.getClass().getName() + ": " + t.getMessage());
            t.printStackTrace(System.out);
            throw t;
        }
    }
}
```
Note: input CQL explicitly uses `frozen<tuple<text, text>>`; the bug strips the `frozen<>`
in RawTuple.toString during the UDT round-trip, so the re-parsed type is the non-frozen
`list<tuple<text, text>>` reported in the error.

## ===== BUGGY RUN (cassandra:3.11.6 jars, pod jdk-buggy on kind-worker) =====
Commands:
```
kubectl cp /tmp/Repro.java repro-15857/jdk-buggy:/tmp/Repro.java -c jdk
kubectl exec jdk-buggy -n repro-15857 -c jdk -- sh -c \
  'cd /tmp && javac -cp "/casslib/*" Repro.java'           # COMPILE_OK
kubectl exec jdk-buggy -n repro-15857 -c jdk -- sh -c \
  'cd /tmp && printf "%s\n" "<configuration><root level=\"OFF\"/></configuration>" > /tmp/lb.xml && \
   java -Dlogback.configurationFile=/tmp/lb.xml -cp "/casslib/*:." Repro 2>/dev/null'
```
Verbatim output (exit code 1):
```
TYPE STMT: CREATE TYPE ks.footype ( f list<frozen<tuple<text, text>>> )
BUILD_FAILED with: org.apache.cassandra.exceptions.InvalidRequestException: Non-frozen tuples are not allowed inside collections: list<tuple<text, text>>
org.apache.cassandra.exceptions.InvalidRequestException: Non-frozen tuples are not allowed inside collections: list<tuple<text, text>>
	at org.apache.cassandra.cql3.CQL3Type$Raw$RawCollection.throwNestedNonFrozenError(CQL3Type.java:705)
	at org.apache.cassandra.cql3.CQL3Type$Raw$RawCollection.prepare(CQL3Type.java:664)
	at org.apache.cassandra.cql3.CQL3Type$Raw$RawCollection.prepareInternal(CQL3Type.java:656)
	at org.apache.cassandra.schema.Types$RawBuilder$RawUDT.lambda$prepare$2(Types.java:313)
	at java.base/java.util.stream.ReferencePipeline$3$1.accept(ReferencePipeline.java:195)
	at java.base/java.util.ArrayList$ArrayListSpliterator.forEachRemaining(ArrayList.java:1655)
	at java.base/java.util.stream.AbstractPipeline.copyInto(AbstractPipeline.java:484)
	at java.base/java.util.stream.AbstractPipeline.wrapAndCopyInto(AbstractPipeline.java:474)
	at java.base/java.util.stream.ReduceOps$ReduceOp.evaluateSequential(ReduceOps.java:913)
	at java.base/java.util.stream.AbstractPipeline.evaluate(AbstractPipeline.java:234)
	at java.base/java.util.stream.ReferencePipeline.collect(ReferencePipeline.java:578)
	at org.apache.cassandra.schema.Types$RawBuilder$RawUDT.prepare(Types.java:314)
	at org.apache.cassandra.schema.Types$RawBuilder.build(Types.java:263)
	at org.apache.cassandra.io.sstable.CQLSSTableWriter$Builder.createTypes(CQLSSTableWriter.java:563)
	at org.apache.cassandra.io.sstable.CQLSSTableWriter$Builder.build(CQLSSTableWriter.java:538)
	at Repro.main(Repro.java:22)
command terminated with exit code 1
```
This matches the Jira body verbatim (same exception type, same message
"Non-frozen tuples are not allowed inside collections: list<tuple<text, text>>", same
RawCollection.throwNestedNonFrozenError frame and same CQLSSTableWriter$Builder.createTypes
-> build chain). Line numbers differ by a few (3.11.6 jar vs the patch context in the report),
but the failing frames and message are identical.

## ===== FIXED CONTROL (cassandra:3.11.8 jars, pod jdk-fixed on kind-worker2) =====
Identical Repro.java, identical CQL.
Verbatim output (exit code 0):
```
TYPE STMT: CREATE TYPE ks.footype ( f list<frozen<tuple<text, text>>> )
BUILD_OK: CQLSSTableWriter constructed without exception
EXIT_CODE=0
```
=> The fix (3.11.8) makes RawTuple.toString emit the `frozen<>` wrapper, so the round-tripped
type stays `list<frozen<tuple<text,text>>>` and CQLSSTableWriter.build() succeeds. Clean A/B.

## Teardown
- `kubectl delete ns repro-15857 --wait=false`
- Loaded JDK image (eclipse-temurin:11) into kind-worker/kind-worker2 node image stores; this is
  node-level cache (not a namespace), harmless and shared. No pre-existing namespace touched.
