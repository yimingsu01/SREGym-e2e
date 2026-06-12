# CASSANDRA-19166 — StackOverflowError on ALTER after many previous schema changes

## Primary source (Jira ground truth)
- summary: "StackOverflowError on ALTER after many previous schema changes"
- components: Cluster/Schema
- fixVersions: 4.1.4, 5.0-rc1, 5.0
- description: Since 4.1, `TableMetadataRefCache` re-wraps its fields in `Collections.unmodifiableMap`
  on every local schema update. This causes the Map fields to reference chains of nested
  UnmodifiableMaps. Eventually leads to a `StackOverflowError` on `get()`, which must traverse lots
  of these maps to fetch the value. Goes away on restart (cache reloaded from disk). Discovered on a
  real test cluster where schema changes were failing, via a heap dump.

## Buggy version & control
- BUGGY: cassandra:4.1.3  (image present on kind node kind-worker2, sha256:7cbcec00...)
- CONTROL (fix): cassandra:4.1.4 (4.1.3 patch+1 = 4.1.4 <= ceiling 11; 4.1.4 is a listed fixVersion).

## Topology decision
- 1 node. The cache (`TableMetadataRefCache`) is a per-node in-memory structure mutated on each LOCAL
  schema update; bug "goes away on restart". No ring/gossip needed. Classifier hint (1node, H) is
  CORRECT. tag_correction = none.

## Mechanism confirmed from buggy 4.1.3 source
File: src/java/org/apache/cassandra/schema/TableMetadataRefCache.java @ cassandra-4.1.3 tag.
- Constructor (lines 53-55): `this.metadataRefs = Collections.unmodifiableMap(metadataRefs);` (also
  metadataRefsByName, indexMetadataRefs).
- withUpdatedRefs() line 80: `hasCreatedOrDroppedTablesOrViews = tablesDiff.created>0 || dropped>0 || ...`
- line 83: `Map metadataRefs = hasCreatedOrDroppedTablesOrViews ? Maps.newHashMap(this.metadataRefs) : this.metadataRefs;`
  => For an ALTER (which is `tablesDiff.altered`, NOT created/dropped) the flag is FALSE, so the
  ALREADY-unmodifiable `this.metadataRefs` is passed THROUGH WITHOUT COPY.
- line 110 -> constructor line 53: wraps it AGAIN: `Collections.unmodifiableMap(<alreadyUnmodifiable>)`.
  => each ALTER adds one nesting layer. `UnmodifiableMap.get()` delegates to the wrapped map's get();
     after N ALTERs depth=N; a `get()` recurses N deep -> StackOverflowError.
- line 102: `tablesDiff.altered.forEach(diff -> metadataRefs.get(diff.after.id).set(diff.after));`
  => the ALTER ITSELF calls get() on the nested map, so a sufficiently-deep ALTER self-faults.
- JVM stack size on this image: -Xss256k (from /etc/cassandra/jvm-server.options).

## Reproducer
Single 4.1.3 pod (pinned to kind-worker2, imagePullPolicy IfNotPresent). Unique keyspace
`repro19166_ks`, table `t`. Run a long sequence of `ALTER TABLE repro19166_ks.t WITH
gc_grace_seconds = <distinct increasing N>;` (each is a real `altered` diff, no no-op short-circuit)
via `cqlsh -f`. Nesting is cumulative in the live process. The fault surfaces as a StackOverflowError
on a schema/Native-Transport thread.

## Commands & raw outputs
(see below; verbatim signature captured from server log / cqlsh)

