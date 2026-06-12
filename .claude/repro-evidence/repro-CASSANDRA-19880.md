# CASSANDRA-19880 — Reproduction Evidence Log

## Bug summary (from Jira ground truth)
**Title:** With enableTracing set to true, the unset() method of a BoundStatement for a map type field failed during execution
**Buggy version:** 5.0.0  | **Fixed in:** 4.0.14, 4.1.7, 5.0.1, 6.0-alpha1, 6.0
**Component:** Legacy/Observability
**Mechanism:** A prepared (bound) statement that leaves a *collection* (e.g. `map<text,text>`) column UNSET, executed with **per-request tracing enabled**, fails server-side. `ExecuteMessage.traceQuery` formats the bound values into the trace string; for the UNSET collection it calls `CQL3Type$Collection.toCQLLiteral` → `CollectionSerializer.readCollectionSize`, which tries to read the 4-byte element count out of the empty UNSET sentinel buffer → `IndexOutOfBoundsException`.

## Tag correction
Classifier HINT: topology=1node, confidence=H, trigger "Prepared statement + UNSET a map column + tracing enabled -> execute -> IndexOutOfBoundsException". **Body confirms the hint exactly.** Single node is correct (pure CQL transport path, no ring/gossip). One nuance worth recording: "tracing enabled" here is a **per-request driver flag** (`session.execute(bound, trace=True)`), NOT a server config / `setraceprobability` — cassandra.yaml was untouched.

## Topology
Single Cassandra pod per version in namespace `repro-19880` on the existing kind cluster (context kind-kind). Two pods deployed in parallel for the A/B control:
- `cass-500`  image `cassandra:5.0.0` (BUGGY)
- `cass-501`  image `cassandra:5.0.1` (FIXED control; ceiling for 5.0 line is 8, so 5.0.1 image exists)

Keyspace `repro19880ks`, table `t (id int PRIMARY KEY, m map<text,text>)`.

## Reproducer (exact)
Driver: the cqlsh-bundled Python driver already inside the pod (`/opt/cassandra/lib/cassandra-driver-internal-only-3.29.0.zip`), added to `sys.path` exactly the way `bin/cqlsh.py` does it (no pip install). Script `/tmp/repro19880.py`:

```python
from cassandra.cluster import Cluster
from cassandra.query import UNSET_VALUE
cluster = Cluster(['127.0.0.1']); session = cluster.connect()
session.execute("CREATE KEYSPACE IF NOT EXISTS repro19880ks WITH replication={'class':'SimpleStrategy','replication_factor':1}", timeout=30)
session.set_keyspace('repro19880ks')
session.execute("CREATE TABLE IF NOT EXISTS t (id int PRIMARY KEY, m map<text,text>)", timeout=30)
ins = session.prepare("INSERT INTO t (id, m) VALUES (?, ?)")
bound = ins.bind([1, UNSET_VALUE])          # leave the map<text,text> column UNSET
rs = session.execute(bound, trace=True, timeout=30)   # tracing ON -> triggers traceQuery on UNSET collection
```

Run inside each pod:
`kubectl exec -n repro-19880 <pod> -- env KS=repro19880ks python3 /tmp/repro19880.py`

## BUGGY result — cass-500 (cassandra:5.0.0)

Client-side:
```
DRIVER IMPORT OK; UNSET_VALUE=<object object at 0x7f9d566e8d60>
SCHEMA READY
BOUND with UNSET map value; executing with trace=True ...
CLIENT EXCEPTION TYPE: NoHostAvailable
CLIENT EXCEPTION MSG : ('Unable to complete the operation against any hosts',
   {<Host: 127.0.0.1:9042 dc1>: <Error from server: code=0000 [Server error] message="java.lang.IndexOutOfBoundsException">})
```

Server-side (`/opt/cassandra/logs/system.log`) — VERBATIM, exact frame chain matches the Jira report:
```
java.lang.IndexOutOfBoundsException: null
	at java.base/java.nio.Buffer.checkIndex(Unknown Source)
	at java.base/java.nio.HeapByteBuffer.getInt(Unknown Source)
	at org.apache.cassandra.utils.ByteBufferUtil.toInt(ByteBufferUtil.java:476)
	at org.apache.cassandra.db.marshal.ByteBufferAccessor.toInt(ByteBufferAccessor.java:208)
	at org.apache.cassandra.db.marshal.ByteBufferAccessor.toInt(ByteBufferAccessor.java:42)
	at org.apache.cassandra.serializers.CollectionSerializer.readCollectionSize(CollectionSerializer.java:74)
	at org.apache.cassandra.cql3.CQL3Type$Collection.toCQLLiteral(CQL3Type.java:221)
	at org.apache.cassandra.transport.messages.ExecuteMessage.traceQuery(ExecuteMessage.java:227)
	at org.apache.cassandra.transport.messages.ExecuteMessage.execute(ExecuteMessage.java:159)
	at org.apache.cassandra.transport.Message$Request.execute(Message.java:259)
	at org.apache.cassandra.transport.Dispatcher.processRequest(Dispatcher.java:416)
	at org.apache.cassandra.transport.Dispatcher.processRequest(Dispatcher.java:435)
	at org.apache.cassandra.transport.Dispatcher.processRequest(Dispatcher.java:462)
	at org.apache.cassandra.transport.Dispatcher$RequestProcessor.run(Dispatcher.java:307)
	at org.apache.cassandra.concurrent.FutureTask$1.call(FutureTask.java:99)
	at org.apache.cassandra.concurrent.FutureTask.call(FutureTask.java:61)
	at org.apache.cassandra.concurrent.FutureTask.run(FutureTask.java:71)
	at org.apache.cassandra.concurrent.SEPWorker.run(SEPWorker.java:143)
	at io.netty.util.concurrent.FastThreadLocalRunnable.run(FastThreadLocalRunnable.java:30)
	at java.base/java.lang.Thread.run(Unknown Source)
```
(Line numbers 74/221/227 vs the Jira-pasted 147/222/223 differ only because the report's paste was from a slightly different build; the discriminating frame chain `readCollectionSize` <- `CQL3Type$Collection.toCQLLiteral` <- `ExecuteMessage.traceQuery` is identical.)

## CONTROL result — cass-501 (cassandra:5.0.1, fixed) — identical script
```
DRIVER IMPORT OK; UNSET_VALUE=<object object at 0x7f629341cd60>
SCHEMA READY
BOUND with UNSET map value; executing with trace=True ...
RESULT: execution SUCCEEDED (no exception)
TRACE OK, request_type=Execute CQL3 prepared query, duration=0:00:00.006904
```
Server log on 5.0.1: `grep -c IndexOutOfBoundsException` = **0**, no ERROR lines from the test. The UNSET-collection trace formats cleanly.

## Disposition: REPRODUCED
Verbatim buggy signature present server-side; identical workload on the fixed image (5.0.1) succeeds and produces a clean trace. Deterministic, single node, no special timing/partition needed.

`verbatim_signature`:
`at org.apache.cassandra.serializers.CollectionSerializer.readCollectionSize(CollectionSerializer.java:74)` (within the `ExecuteMessage.traceQuery` -> `CQL3Type$Collection.toCQLLiteral` chain raising `java.lang.IndexOutOfBoundsException`)

## Commands (reproducible)
```
kubectl create ns repro-19880
# deploy cass-500 (cassandra:5.0.0) and cass-501 (cassandra:5.0.1), single-pod template
kubectl wait -n repro-19880 --for=condition=Ready pod/cass-500 pod/cass-501 --timeout=300s
# driver path replicated from bin/cqlsh.py: <zip>/cassandra-driver-<ver> + pure_sasl/wcwidth/geomet zips
kubectl cp /tmp/repro19880.py repro-19880/cass-500:/tmp/repro19880.py
kubectl exec -n repro-19880 cass-500 -- env KS=repro19880ks python3 /tmp/repro19880.py   # -> IOOBE
kubectl exec -n repro-19880 cass-500 -- grep -A22 IndexOutOfBoundsException /opt/cassandra/logs/system.log
kubectl cp /tmp/repro19880.py repro-19880/cass-501:/tmp/repro19880.py
kubectl exec -n repro-19880 cass-501 -- env KS=repro19880ks python3 /tmp/repro19880.py   # -> SUCCEEDED
# teardown
kubectl delete ns repro-19880 --wait=false
```

## Tooling findings
- The cassandra:5.0.x image has no `unzip` and no system-installed cassandra-driver. Reproducing UNSET (a protocol sentinel, unreachable from cqlsh) requires the bundled driver; importing it directly from the on-disk zips on `sys.path` (the cqlsh.py approach) works without network/pip. Worth baking into the skill for any UNSET / prepared-statement-binding Cassandra reproducer.
- First DDL attempt at the driver default 10s timeout hit `OperationTimedOut` on a freshly-started node; bumping execute timeout to 30s resolved it (not bug-related).
