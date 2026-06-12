# CASSANDRA-17919 — Reproduction Evidence

**Summary:** Capital `P` gets confused in the CQL parser for a Duration in places where IDENT are needed.
**Component:** CQL/Syntax
**Buggy version:** 4.1.1   **Fixed (A/B control image):** 4.1.2
**Fix versions (per Jira):** 3.11.15, 4.0.10, 4.1.2, 5.0-alpha1, 5.0
**Topology:** 1 node (HINT confirmed by body — no ring needed)
**Namespace:** repro-17919   **Keyspace:** repro17919 (RF=1, SimpleStrategy)
**Disposition: REPRODUCED**

## Root cause (from body)
The Jira body provides two reproducers. The original was found via Accord `LET` transaction syntax
(`LET P = (SELECT ...)`), which is NOT present in 4.1.1. The body then gives a **version-agnostic**
reproducer that exercises the same parser defect on stock Cassandra:

```
cqlsh:ks> CREATE TABLE P (k INT PRIMARY KEY);
SyntaxException: line 1:13 no viable alternative at input 'P' (CREATE TABLE [P]...)
cqlsh:ks> CREATE TABLE p (k INT PRIMARY KEY);
cqlsh:ks>
```

A bare capital `P` is mis-lexed as the leading token of an ISO-8601 Duration literal (e.g. `P1Y2M`),
so it cannot be consumed where the grammar expects a plain identifier (IDENT). Lowercase `p` is
unaffected. This is a server-side parse error surfaced through cqlsh.

## Environment / version verification
```
$ kubectl get pod -n repro-17919 cass        -> image cassandra:4.1.1  (sha256:7772bea6...)
$ kubectl get pod -n repro-17919 cass-fixed  -> image cassandra:4.1.2  (sha256:f100c4c5...)

buggy  release_version = 4.1.1
fixed  release_version = 4.1.2
```

## Reproducer + controls (verbatim outputs from THIS run)

### 1. BUGGY 4.1.1 — reproducer (capital P)
Command:
```
kubectl exec -n repro-17919 cass -- cqlsh -k repro17919 -e "CREATE TABLE P (k INT PRIMARY KEY)"
```
Output:
```
<stdin>:1:SyntaxException: line 1:13 no viable alternative at input 'P' (CREATE TABLE [P]...)
command terminated with exit code 2
```
=> Matches the Jira body exactly (line 1:13, identical message). **BUGGY SIGNATURE.**

### 2. WITHIN-VERSION control (SAME 4.1.1 binary) — lowercase p succeeds
Command:
```
kubectl exec -n repro-17919 cass -- cqlsh -k repro17919 -e "CREATE TABLE p (k INT PRIMARY KEY)"   # exit=0
kubectl exec -n repro-17919 cass -- cqlsh -k repro17919 -e "SELECT table_name FROM system_schema.tables WHERE keyspace_name='repro17919'"
```
Output:
```
 table_name
------------
          p
(1 rows)
```
=> Identical statement differing only in case succeeds on the identical buggy binary. Isolates
**case (capital P)** as the sole trigger.

### 3. A/B IMAGE control (4.1.2) — same capital-P statement succeeds
Command:
```
kubectl exec -n repro-17919 cass-fixed -- cqlsh -k repro17919 -e "CREATE TABLE P (k INT PRIMARY KEY)"  # exit=0
kubectl exec -n repro-17919 cass-fixed -- cqlsh -k repro17919 -e "SELECT table_name FROM system_schema.tables WHERE keyspace_name='repro17919'"
```
Output:
```
 table_name
------------
          p
(1 rows)
```
=> On 4.1.2 the capital-`P` CREATE TABLE parses fine (unquoted identifier folds to lowercase `p`).
The fix resolves the defect.

## Conclusion
- Buggy 4.1.1 throws `SyntaxException: line 1:13 no viable alternative at input 'P' (CREATE TABLE [P]...)`.
- Lowercase `p` works on the same 4.1.1 binary (case is the trigger).
- The identical capital-`P` statement works on fixed 4.1.2.
All three legs reproduced cleanly => **REPRODUCED**.

## Tag correction
Classifier hint trigger phrased the error as "no viable alternative at input P (capital P parsed as
Duration)" with topology=1node, confidence=H. The body confirms all of this. One nuance: the hint's
verbatim error string differs slightly from the actual one. Hint said the message is about P "parsed as
Duration"; the actual cqlsh message is `no viable alternative at input 'P' (CREATE TABLE [P]...)` —
"parsed as Duration" is the root-cause explanation, not the literal output. Topology (1node) and
confidence (H) were correct.
