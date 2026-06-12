# CASSANDRA-17840 Reproduction Evidence Log

## Bug
**Summary:** IndexOutOfBoundsException in Paging State Version Inference (V3 State Received on V4 Connection)
**Component:** Messaging/Client
**Buggy version tested:** cassandra:4.0.5 (also confirmed buggy: cassandra:4.0.6)
**Fixed version (A/B control):** cassandra:4.0.7
**JIRA fixVersions field:** 3.11.14, 4.0.6, 4.1-beta1, 5.0-alpha1, 5.0
**ACTUAL first fixed 4.0.x release:** 4.0.7 (per CHANGES.txt + source diff — see Tag Correction)

## Primary-source mechanism (from JIRA body + source)
JIRA description: "In PagingState.java, `index` is an integer field, and we add long values to
it without a `Math.toIntExact` check. While we're checking for negative return values returned by
`getUnsignedVInt`, there's a chance that the value returned by it is so large that addition operation
would cause integer overflow, or the value itself is large enough to cause overflow."

Buggy code in `PagingState.isModernSerialized` (cassandra-4.0.5 / 4.0.6 source, verbatim):
```java
int index = bytes.position();
int limit = bytes.limit();
long partitionKeyLen = getUnsignedVInt(bytes, index, limit);
if (partitionKeyLen < 0)
    return false;
index += computeUnsignedVIntSize(partitionKeyLen) + partitionKeyLen;  // int += long  -> OVERFLOW (no Math.toIntExact)
if (index >= limit)                                                    // negative index passes this check
    return false;
long rowMarkerLen = getUnsignedVInt(bytes, index, limit);              // called with NEGATIVE index
```
`getUnsignedVInt(ByteBuffer, readerIndex, readerLimit)` only guards `if (readerIndex >= readerLimit) return -1;`
A negative `readerIndex` passes that guard, then `input.get(readerIndex)` throws IndexOutOfBoundsException.
This is NOT an IOException, so it escapes `deserialize`'s `catch (IOException)` and surfaces to the client
as a SERVER_ERROR (opcode 0x00, error code 0x00000000) rather than a clean PROTOCOL_ERROR.

`deserialize()` on a V4+ connection calls `isModernSerialized(bytes)` FIRST, so a client merely needs to
send a crafted `paging_state` over a V4 native protocol QUERY to hit this.

## Exact reproducer extracted / derived
Send a CQL native protocol **v4** QUERY with the `with_paging_state` flag (0x08) set and a crafted
`paging_state` whose first unsigned-VInt (partitionKeyLen) encodes a very large positive value so that
`index += vintsize + partitionKeyLen` overflows `int` to a negative number.
- paging_state bytes = `f0 7f ff ff ff` (5-byte unsigned VInt decoding to partitionKeyLen = 2147483647 = 0x7FFFFFFF)
- index = position(0) + computeUnsignedVIntSize(2147483647)=5 + 2147483647 = 2147483652
- (int)2147483652 = -2147483644  -> passes `index >= limit` check -> get(-2147483644) -> IndexOutOfBoundsException

No bulk data required. A 5-row table is used only so a *valid* paging_state control also returns rows.

## Environment
- Existing kind cluster (context kind-kind, 4 nodes). Namespace created: `repro-17840`. Keyspace: `repro17840`.
- Single Cassandra pod per version. Raw native-protocol client written in pure Python stdlib
  (`/tmp/repro17840c.py`, copied into each pod and run against 127.0.0.1:9042 so the connection
  negotiates native protocol v4). cqlsh cannot inject a raw arbitrary paging_state, hence the raw client.

## Setup commands
```
kubectl create ns repro-17840
# deploy pod 'cass' image cassandra:4.0.5 (single-node template), wait Ready, poll cqlsh
kubectl exec -n repro-17840 cass -- cqlsh -e "
  CREATE KEYSPACE IF NOT EXISTS repro17840 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
  CREATE TABLE IF NOT EXISTS repro17840.t (id int PRIMARY KEY, v text);
  INSERT ... (1,'a')..(5,'e');"   # -> SELECT count(*) = 5
```
First a VALID modern paging_state was obtained from the server (page_size=2 query) to validate the frame
format: real paging_state = `040000000100f07ffffffd00` (resumes correctly -> RESULT kind=2).

## Reproduction run (BUGGY cassandra:4.0.5)
Command:
```
kubectl exec -n repro-17840 cass -- python3 /tmp/repro17840c.py
```
Raw output:
```
[valid-real-state] reply opcode=0x08 len=107
[valid-real-state] RESULT kind=2
malicious paging_state hex=f07fffffff (pkLen=2147483647, vintsize=5, index=2147483652 -> int wrap=-2147483644)
[MALICIOUS-overflow] reply opcode=0x00 len=54
[MALICIOUS-overflow] ERROR code=0x00000000 message='java.lang.IndexOutOfBoundsException: -2147483644'
```
BUGGY SIGNATURE (verbatim, from server over native protocol):
```
java.lang.IndexOutOfBoundsException: -2147483644
```
(opcode 0x00 = ERROR frame; error code 0x00000000 = SERVER_ERROR. The value -2147483644 is exactly the
int-overflowed `index`, confirming the described integer overflow.)

## A/B control #1 (cassandra:4.0.6 — the task's "buggy patch+1" candidate)
Identical workload, raw output:
```
[valid-real-state] RESULT kind=2
[MALICIOUS-overflow] reply opcode=0x00 len=54
[MALICIOUS-overflow] ERROR code=0x00000000 message='java.lang.IndexOutOfBoundsException: -2147483644'
```
=> 4.0.6 STILL EXHIBITS THE BUG (identical signature). The 4.0.6 source still has
`index += computeUnsignedVIntSize(partitionKeyLen) + partitionKeyLen` with `int index`, no guard.

## A/B control #2 (FIXED cassandra:4.0.7)
Image pulled, `kind load`ed, pod `cass7` (confirmed `cassandra -v` = 4.0.7). Identical workload, raw output:
```
[valid-real-state] reply opcode=0x08 len=107
[valid-real-state] RESULT kind=2
malicious paging_state hex=f07fffffff (pkLen=2147483647, vintsize=5, index=2147483652 -> int wrap=-2147483644)
[MALICIOUS-overflow] reply opcode=0x00 len=40
[MALICIOUS-overflow] ERROR code=0x0000000a message='Invalid value for the paging state'
```
=> FIXED. 4.0.7 returns PROTOCOL_ERROR (code 0x0000000a) with clean message "Invalid value for the
paging state" instead of leaking the raw IndexOutOfBoundsException as a SERVER_ERROR.

4.0.7 `isModernSerialized` source (verbatim) shows the fix:
```java
int partitionKeyLen = toIntExact(getUnsignedVInt(bytes, index, limit));
if (partitionKeyLen < 0) return false;
index = addNonNegative(index, computeUnsignedVIntSize(partitionKeyLen), partitionKeyLen);
if (index >= limit || index < 0) return false;     // new: index < 0 guard
...
```
`toIntExact` throws on overflow (-> IOException -> ProtocolException), and `addNonNegative` + the
`index < 0` check prevent the negative-index read.

## Tag correction
- Classifier hint topology=1node, trigger "resume paging with V3 PagingState over V4 connection ->
  integer overflow -> IndexOutOfBoundsException": CONFIRMED ACCURATE (single node; V4 connection;
  crafted paging_state; int overflow; IndexOutOfBoundsException).
- JIRA `fixVersions` lists 4.0.6 but the fix is NOT in 4.0.6 (empirically reproduces identically, and
  4.0.6 source is unchanged). The real first fixed 4.0.x release is **4.0.7** (CHANGES.txt:
  "Fix potential IndexOutOfBoundsException in PagingState in mixed mode clusters (CASSANDRA-17840)").
  The task's suggested fixed-control image "buggy patch+1" = 4.0.6 is therefore NOT a valid fixed control;
  4.0.7 is. This is the meaningful correction.

## Disposition: reproduced
Verbatim buggy signature: `java.lang.IndexOutOfBoundsException: -2147483644`
Clean A/B contrast vs fixed 4.0.7 (`Invalid value for the paging state`, PROTOCOL_ERROR 0x0a).

## Teardown
`kubectl delete ns repro-17840 --wait=false` (performed after writing this log).
