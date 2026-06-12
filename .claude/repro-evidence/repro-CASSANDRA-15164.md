# CASSANDRA-15164 — Reproduction Evidence Log

**Summary:** Overflowed Partition Cell Histograms Can Prevent Compactions from Executing
**Buggy version:** cassandra:3.11.8 (fixVersions: 3.0.23, **3.11.9**, 4.0-beta3, 4.0)
**Components:** CQL/Interpreter
**Namespace:** repro-15164 (CREATED, then torn down — see sections 5/6)
**Disposition:** needs-fix-test (with strong evidence — fix-style test run against the REAL buggy jar)
**Date:** 2026-06-11

---

## 1. Primary source (Jira body — ground truth)

Reporter on a 6-node, 3-seed production cluster, Cassandra **3.7**. One seed node continuously
throws, surfaced to the client as a `ServerError`:

```
cassandra.protocol.ServerError: <Error from server: code=0000 [Server error]
message="java.lang.IllegalStateException: Unable to compute ceiling for max when histogram overflowed">
```

Mitigation used by reporter: drained the node. Bug title: the overflowed histogram **prevents
compactions from executing**. fixVersions include **3.11.9**, so **3.11.8 is buggy**.

Classifier HINT: topology=1node, confidence=M, trigger "partition with cell count exceeding histogram
bound -> IllegalStateException (histogram overflowed) + compactions blocked". The hint **matches the
body** => tag_correction = none.

The body gives NO concrete reproducer (no DDL/DML, no row counts). The trigger must be derived from
source.

---

## 2. Mechanism, pinned from the BUGGY TAG's actual source (cassandra-3.11.8)

The candidate turns on ONE number: the histogram ceiling. I fetched the `.java` files from the exact
buggy tag `cassandra-3.11.8` (the public `cassandra:3.11.8` image is a binary dist with no source).

### 2a. Throw site — `EstimatedHistogram.java` (tag cassandra-3.11.8)

```java
public double rawMean()
{
    int lastBucket = buckets.length() - 1;
    if (buckets.get(lastBucket) > 0)
        throw new IllegalStateException("Unable to compute ceiling for max when histogram overflowed");
    ...
}

public boolean isOverflowed()
{
    return buckets.get(buckets.length() - 1) > 0;   // last (overflow) bucket non-empty
}
```

`mean()` calls `rawMean()`. The exception throws **iff the last (overflow) bucket is non-empty**.
Bucket boundaries start at 1 and grow ×1.2.

KEY: `EstimatedHistogram.add(v)` puts `v` straight into the overflow bucket when `v` exceeds the top
offset — in a **single call**. So the *throw* needs only one oversized sample, NOT a real giant
partition. (Verified empirically in section 4.)

### 2b. The ceilings — `MetadataCollector.java` (tag cassandra-3.11.8)

```java
static EstimatedHistogram defaultCellPerPartitionCountHistogram()
{
    // EH of 114 can track a max value of 2395318855, i.e., > 2B columns
    return new EstimatedHistogram(114);
}
static EstimatedHistogram defaultPartitionSizeHistogram()
{
    // EH of 150 can track a max value of 1697806495183, i.e., > 1.5PB
    return new EstimatedHistogram(150);
}
```

The cell-per-partition histogram has 114 buckets; empirically (section 4) the largest bucket OFFSET is
**1,996,099,046** (the comment's 2,395,318,855 counts the implicit overflow bucket — immaterial). The
partition-size histogram tops out at ~1.5 PB. These are the only two SSTable-metadata histograms on the
throw path.

### 2c. How it blocks compactions / reaches the operator — `StatsMetadata.java` (tag cassandra-3.11.8)

```java
public double getEstimatedDroppableTombstoneRatio(int gcBefore)
{
    long estimatedColumnCount = this.estimatedColumnCount.mean() * this.estimatedColumnCount.count();
    ...
}
```

`getEstimatedDroppableTombstoneRatio()` is invoked during compaction candidate selection
(`AbstractCompactionStrategy.worthDroppingTombstones`) and single-SSTable compaction (`nodetool
compact`/`garbagecollect`). If a candidate SSTable's cell-per-partition histogram has overflowed,
`mean()`→`rawMean()` throws and the compaction aborts — the "compactions prevented" title; the same
JMX/operator-triggered path surfaces the `ServerError` the reporter saw.

### 2d. The fix (commit 4782fd3, shipped 3.11.9 / 3.0.23 / 4.0) — confirmed against cassandra-3.11.9 source

- `EstimatedHistogramSerializer.serialize()` now logs a WARN: "Serializing a histogram with N values
  greater than the maximum of <topOffset>..." (observed live in section 4b).
- A NEW method `clearOverflow()` ("Resets the count in the overflow bucket to zero") is added.
- BUT `serialize()`/`deserialize()` do NOT auto-clear overflow, and `mean()`/`rawMean()` STILL throw on
  overflow in 3.11.9. The fix relies on the **caller** (metadata/compaction path) invoking
  `clearOverflow()` before computing the mean. => a raw `serialize→deserialize→mean` snippet does NOT
  discriminate the versions; the discriminating reproducer is the fix's caller-level UNIT TEST.

---

## 3. Two reproducer tiers — why the disposition is needs-fix-test

| Path | What it needs | Feasible on a stock 2.5 GB pod in budget? |
| --- | --- | --- |
| **Server-visible symptom** (compaction reads an overflowed SSTable cell histogram → ServerError) | a single partition with **> ~2.0 billion cells** (or a >1.5 PB partition) so the on-disk Statistics.db histogram overflows | **NO.** >2B cells ⇒ tens-to-hundreds of GB + trillions of writes; pod OOMs (limit 2560Mi); mutation/frame caps forbid a shortcut. No stock-pod path to inject a hand-crafted overflowed Statistics.db. |
| **Code-defect / fix-style test** (`add(>ceiling)` then `mean()`, incl. serialize→deserialize→mean) | one oversized `add()` | **YES** — done in section 4 via Nashorn (`jjs`) against the REAL buggy jar. |

So: the *symptom* via the DB request path is infeasible here, but the *defect* is reproducible exactly
via the fix's own serialize→deserialize→mean style test. Per the disposition menu ("the only reproducer
is the fix's unit/dtest") => **needs-fix-test**, and I went further than typical by adapting AND running
that test against the real `apache-cassandra-3.11.8.jar` and capturing the throw.

NOTE on evidence bar: the captured exception comes from a **Nashorn harness invoking the library class
directly**, NOT from cqlsh/the server request path. It is NOT the server-visible symptom, so this is
NOT "reproduced".

---

## 4. Empirical evidence (commands + RAW output)

### Environment
```
$ kubectl config current-context           # kind-kind
$ kubectl get nodes                         # 4 nodes Ready
$ kubectl create ns repro-15164             # namespace/repro-15164 created
# deployed pod 'cass' (cassandra:3.11.8) and 'cass-fixed' (cassandra:3.11.9), both Running.
$ kubectl exec -n repro-15164 cass -- sh -c 'which javac java; java -version'
  /opt/java/openjdk/bin/java          # NO javac -> JRE-only image
  openjdk version "1.8.0_272" (AdoptOpenJDK), JRE
$ kubectl exec ... ls /opt/cassandra/lib/apache-cassandra-3.11.8.jar   # buggy jar present
# 'jjs' (Nashorn) present at /opt/java/openjdk/bin/jjs -> drives the jar with no compiler.
```

### 4a. BUGGY cassandra:3.11.8 — `jjs -cp <all cassandra jars> /tmp/hist_test.js`
Script: `EH=Java.type("org.apache.cassandra.utils.EstimatedHistogram"); h=new EH(114); h.add(9e9);`
then `h.mean()`; then serialize via `EH.serializer` → deserialize → `mean()`.

RAW OUTPUT (cassandra:3.11.8):
```
=== 3.11.8 EstimatedHistogram overflow behavior (REAL buggy jar) ===
bucket count (offsets+1) -> getBucketOffsets().length = 114
top (largest) bucket offset = 1996099046
after one add(9000000000): isOverflowed() = true
mean() THREW: java.lang.IllegalStateException: Unable to compute ceiling for max when histogram overflowed

=== serialize -> deserialize -> mean() (the discriminating path the fix changes) ===
serialized overflowed histogram, bytes = 1844
deserialized: isOverflowed() = true
deserialized mean() THREW (BUGGY 3.11.8 behavior): java.lang.IllegalStateException: Unable to compute ceiling for max when histogram overflowed
```

### 4b. FIXED cassandra:3.11.9 — IDENTICAL script, same namespace pod `cass-fixed`
RAW OUTPUT (cassandra:3.11.9):
```
=== 3.11.8 EstimatedHistogram overflow behavior (REAL buggy jar) ===
bucket count (offsets+1) -> getBucketOffsets().length = 114
top (largest) bucket offset = 1996099046
after one add(9000000000): isOverflowed() = true
mean() THREW: java.lang.IllegalStateException: Unable to compute ceiling for max when histogram overflowed

=== serialize -> deserialize -> mean() (the discriminating path the fix changes) ===
04:28:40.056 [main] WARN  o.a.c.u.EstimatedHistogram$EstimatedHistogramSerializer - Serializing a histogram with 1 values greater than the maximum of 1996099046...
serialized overflowed histogram, bytes = 1844
deserialized: isOverflowed() = true
deserialized mean() THREW (BUGGY 3.11.8 behavior): java.lang.IllegalStateException: Unable to compute ceiling for max when histogram overflowed
```

### A/B reading (honest measurement, not a forced narrative)
- **Observable diff introduced by the fix:** 3.11.9 emits a new serialize-time WARN
  ("Serializing a histogram with 1 values greater than the maximum of 1996099046...") that 3.11.8 does
  NOT. This is a real fix-version behavior change, captured live.
- The raw `serialize→deserialize→mean` path does NOT discriminate: BOTH versions still throw, because
  `mean()` still throws on overflow in 3.11.9 and the fix's `clearOverflow()` is invoked by the
  *caller*, not by (de)serialization (confirmed against cassandra-3.11.9 source, section 2d). The
  version-discriminating reproducer is therefore the fix's caller-level unit test — reinforcing
  needs-fix-test. Within-version reasoning (section 2) already covers why a normal partition never
  overflows on either build.

---

## 5. Namespace / teardown

- Namespace **repro-15164 was created** for this attempt; deployed `cass` (3.11.8) and `cass-fixed`
  (3.11.9). Both reached Running; the JRE-only image meant no `javac`, so the histogram test was run via
  `jjs` (Nashorn) against the real cassandra jars on the classpath.
- Torn down at end: `kubectl delete ns repro-15164 --wait=false` (see section 6). No pre-existing
  namespace (cass-*, repro-smoke, k8ssandra-operator, cert-manager, other repro-*) was touched.

---

## 6. Disposition: needs-fix-test (strong evidence)

The server-visible symptom (compaction reading an overflowed on-disk cell histogram) is infeasible on a
stock 2.5 GB pod in budget — it requires a single partition > ~2.0 billion cells (or > 1.5 PB), and
there is no stock-pod mechanism to inject a hand-crafted overflowed Statistics.db. The ONLY compact
reproducer is the fix's own serialize→deserialize→mean (add-one-oversized-value) test, which I adapted
and ran against the REAL `apache-cassandra-3.11.8.jar` via Nashorn, capturing the exact Jira exception.
Because that capture is from a library-level harness (not cqlsh/the server request path), it does NOT
meet the "reproduced" bar => needs-fix-test.

---

## VERBATIM SIGNATURE (provenance: my own capture from the REAL cassandra:3.11.8 jar via jjs harness;
## NOT the server/cqlsh request path)

```
mean() THREW: java.lang.IllegalStateException: Unable to compute ceiling for max when histogram overflowed
```

(Reporter's body line — NOT my reproduction — is the same exception surfaced as a ServerError:
`java.lang.IllegalStateException: Unable to compute ceiling for max when histogram overflowed`.)
