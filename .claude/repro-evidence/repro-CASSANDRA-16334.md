# CASSANDRA-16334 — Reproduction Evidence Log

**Summary (Jira):** "Replica failure causes timeout on multi-DC write"
**Buggy version:** cassandra:4.0.1   **Fixed (A/B control):** cassandra:4.0.2 (fixVersions includes 4.0.2)
**Components:** Consistency/Coordination, Messaging/Internode
**Namespace:** repro-16334 (kind cluster, context kind-kind)
**Disposition:** REPRODUCED (verbatim WriteTimeout code=1100 captured; clean A/B against fixed 4.0.2)

## Bug (from Jira body — ground truth)
Inserting a mutation larger than `max_mutation_size_in_kb`:
- on a **single-DC** keyspace (RF=3) -> correctly `WriteFailure` (code=1500)
- on a **2-DC** keyspace (RF=3 each) -> **wrongly** `WriteTimeout` (code=1100)

The defect: with a DC-local consistency level (LOCAL_ONE, the DataStax-driver default used in the
report), the coordinator's write-response handler counts total replicas across ALL DCs but only tallies
*failure* responses from the local DC. So when every replica rejects the oversized mutation, the failure
threshold is never reached and the coordinator waits out the full write timeout, emitting a WriteTimeout
instead of the correct WriteFailure.

## Tag correction
Classifier hint trigger said "2-DC (RF=3 each)". The bug is the single-DC-vs-multi-DC DIFFERENCE and the
mechanism is RF-independent — it reproduces with RF=1 per DC (minimal 2-node ring), no need for 6 nodes.
topology=ring (multi-DC) and confidence=H were CORRECT.

## Critical reproduction detail
Consistency MUST be LOCAL_ONE. cqlsh defaults to ONE (routes through WriteResponseHandler which counts
all failures -> would give the CORRECT WriteFailure and mask the bug). The Jira signature shows
`'consistency': 'LOCAL_ONE'` (driver default). We set `CONSISTENCY LOCAL_ONE;` before the insert.

## Topology deployed
2 single-node pods, one per DC, GossipingPropertyFileSnitch, `max_mutation_size_in_kb: 1000` appended to
cassandra.yaml on each. Ephemeral storage. Data kept tiny (one ~2.2 MB blob row).

Buggy ring (cluster "repro"):  cass-dc1 (dc1) 10.244.1.130 UN ; cass-dc2 (dc2) 10.244.1.131 UN
Fixed ring (cluster "reprofix"): fix-dc1 (dc1) 10.244.2.108 UN ; fix-dc2 (dc2) 10.244.3.107 UN

nodetool status (buggy ring) showed 2 datacenters, both UN:
```
Datacenter: dc1 ... UN  10.244.1.130  ... rack1
Datacenter: dc2 ... UN  10.244.1.131  ... rack1
```

## Workload (identical across all three runs)
CQL file (written inside the pod to avoid ARG_MAX with a 2 MB inline arg):
```
CONSISTENCY LOCAL_ONE;
INSERT INTO <ks>.t (key, val) VALUES (1, 0x<2,200,000 hex chars = 1.1 MB byte blob ... 'ab'*1100000>);
```
Blob = 1.1 MB of bytes (2.2 MB hex) >> max_mutation_size_in_kb=1000 (~1 MB), so every replica rejects it.
Schema: `CREATE TABLE <ks>.t (key int PRIMARY KEY, val blob);`

-------------------------------------------------------------------------------
## RESULT 1 — BUGGY 4.0.1, MULTI-DC keyspace (dc1:1, dc2:1)  [THE BUG]
Command:
```
kubectl exec -n repro-16334 cass-dc1 -- cqlsh -f /tmp/ins.cql
```
Verbatim output:
```
Consistency level set to LOCAL_ONE.
/tmp/ins.cql:3:WriteTimeout: Error from server: code=1100 [Coordinator node timed out waiting for replica nodes' responses] message="Operation timed out - received only 0 responses." info={'consistency': 'LOCAL_ONE', 'required_responses': 1, 'received_responses': 0, 'write_type': 'SIMPLE'}
command terminated with exit code 2
```
=> WRONG behavior. Matches the Jira "wrongly causes a timeout" symptom (code=1100). THIS IS THE BUG.

-------------------------------------------------------------------------------
## RESULT 2 — BUGGY 4.0.1, SINGLE-DC keyspace (dc1:1)  [report's own contrast]
Same image, same pods, same LOCAL_ONE, same 2.2 MB blob; keyspace replicated to dc1 only.
Command:
```
kubectl exec -n repro-16334 cass-dc1 -- cqlsh -f /tmp/ins_single.cql
```
Verbatim output:
```
Consistency level set to LOCAL_ONE.
/tmp/ins_single.cql:3:WriteFailure: Error from server: code=1500 [Replica(s) failed to execute write] message="Operation failed - received 0 responses and 1 failures: UNKNOWN from /10.244.1.130:7000" info={'consistency': 'LOCAL_ONE', 'required_responses': 1, 'received_responses': 0, 'failures': 1, 'error_code_map': {'10.244.1.130': '0x0000'}}
command terminated with exit code 2
```
=> CORRECT behavior (code=1500 WriteFailure). Only the keyspace topology (1 DC vs 2 DC) differs from
RESULT 1, isolating the bug to the multi-DC coordinator path — exactly as the Jira body describes.

-------------------------------------------------------------------------------
## RESULT 3 — FIXED 4.0.2, MULTI-DC keyspace (dc1:1, dc2:1)  [A/B CONTROL]
Identical topology (2-DC ring) and identical workload as RESULT 1, on the fixed image.
Command:
```
kubectl exec -n repro-16334 fix-dc1 -- cqlsh -f /tmp/ins.cql
```
Verbatim output:
```
Consistency level set to LOCAL_ONE.
/tmp/ins.cql:3:WriteFailure: Error from server: code=1500 [Replica(s) failed to execute write] message="Operation failed - received 0 responses and 1 failures: UNKNOWN from /10.244.2.108:7000" info={'consistency': 'LOCAL_ONE', 'required_responses': 1, 'received_responses': 0, 'failures': 1, 'error_code_map': {'10.244.2.108': '0x0000'}}
command terminated with exit code 2
```
=> CORRECT behavior (code=1500 WriteFailure). The fix in 4.0.2 makes multi-DC behave like single-DC.

-------------------------------------------------------------------------------
## Conclusion
| Image       | Keyspace topology       | Result                  | Verdict           |
|-------------|-------------------------|-------------------------|-------------------|
| 4.0.1 buggy | multi-DC (dc1:1,dc2:1)  | WriteTimeout  code=1100 | WRONG (the bug)   |
| 4.0.1 buggy | single-DC (dc1:1)       | WriteFailure  code=1500 | correct (contrast)|
| 4.0.2 fixed | multi-DC (dc1:1,dc2:1)  | WriteFailure  code=1500 | correct (fix)     |

Same workload, same LOCAL_ONE consistency, same oversized blob across all three. The only variable that
flips correct<->buggy is exactly what CASSANDRA-16334 says: multi-DC on the unfixed code. REPRODUCED.

VERBATIM SIGNATURE (buggy):
WriteTimeout: Error from server: code=1100 [Coordinator node timed out waiting for replica nodes' responses] message="Operation timed out - received only 0 responses." info={'consistency': 'LOCAL_ONE', 'required_responses': 1, 'received_responses': 0, 'write_type': 'SIMPLE'}

## Tooling findings
None. Both cassandra:4.0.1 and cassandra:4.0.2 were already cached on the kind nodes (no pull needed).
The trailing-semicolon SyntaxException in the DESCRIBE/CREATE step is a benign cqlsh artifact of the
heredoc and did not affect schema creation (keyspace/table were created successfully).

## Teardown
Namespace repro-16334 deleted with `kubectl delete ns repro-16334 --wait=false`.
