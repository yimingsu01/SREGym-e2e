# CASSANDRA-16796 — Clear pending ranges for a SHUTDOWN peer

- **Buggy version:** cassandra:4.0.0 (fixed in 3.0.25 / 3.11.11 / **4.0.1**)
- **Component:** Cluster/Membership
- **Namespace:** repro-16796 (kind, context kind-kind)
- **Topology:** 3-node single-token ring (num_tokens=1), RF=2 SimpleStrategy keyspace `repro16796`
- **Date:** 2026-06-12

## Bug (from Jira body — ground truth)
> If a node involved in a MOVE operation should fail, peers can sometimes maintain pending ranges for it
> even when it has left the ring and/or been replaced... A graceful shutdown causes that [MOVING] status to
> be replaced with SHUTDOWN, but doesn't update TokenMetadata, so pending ranges remain for the down node...
> This in turn can lead to bogus unavailable responses to clients if a replica for any of the pending ranges
> should go down.

Fix commit fbb20b9162b73c4de8a82cf4ffdde3304e904603 changes `Gossiper.markAsShutdown()` to additionally
call `subscriber.onChange(endpoint, ApplicationState.STATUS, shutdown)` so TokenMetadata clears the MOVING
status / pending ranges. In 4.0.0 that notification is MISSING, so the MOVING state + pending ranges persist
after a graceful shutdown.

## Tag correction
Classifier hint trigger was accurate (MOVE + graceful shutdown of moving node -> stale pending ranges ->
bogus unavailable). Topology hint=ring is correct, BUT the ring must be **single-token** (num_tokens=1):
with the Docker image's default vnodes (16 tokens) `nodetool move` is rejected
("This node has more than one token and cannot be moved thusly."). Not an in-JVM-only bug — reproduced on a
real ring by widening the MOVING window with throttled streaming + catching MOVING at graceful shutdown.

## Reproducer steps
1. 3-node single-token ring on cassandra:4.0.0 (StatefulSet, ephemeral storage, terminationGraceperiod=90s
   so the SHUTDOWN gossip state is announced rather than SIGKILLed).
2. Keyspace repro16796 RF=2, table t(id int PK, v text), 30 rows.
3. `nodetool setstreamthroughput 1` on all nodes (widen MOVING window).
4. Tokens: cass-2=-3584644331145400280, cass-1=813234791936175363, cass-0=8051695314435402860.
   On cass-2 (highest-ownership node): `nodetool move 3000000000000000000` in background. Target 3e18 lies
   between cass-1 and cass-0 -> cass-2 becomes a PENDING replica for range (813234791936175363, 3e18].
5. Poll `nodetool status` until cass-2 = `UM` (Up/Moving). Peers now hold pending ranges for cass-2.
6. While MOVING, gracefully shut down cass-2: `kubectl scale statefulset cass --replicas=2` (SIGTERM ->
   drain -> gossip SHUTDOWN).

## VERBATIM buggy evidence

### (a) cass-2 announced graceful SHUTDOWN (not a hard crash) — peer gossipinfo on cass-0
```
/10.244.2.132
  generation:1781256306
  heartbeat:2147483647
  STATUS_WITH_PORT:410:shutdown,true
```

### (b) BUT cass-0's TokenMetadata still shows cass-2 as MOVING -> `DM` (Down + Moving)
`kubectl exec -n repro-16796 cass-0 -- nodetool status repro16796`
```
--  Address       Load       Tokens  Owns (effective)  Host ID                               Rack
UN  10.244.3.126  96.13 KiB  1       60.8%             6569297b-4682-481e-a4f6-a7161d41b6a1  rack1
DM  10.244.2.132  91.16 KiB  1       76.2%             dd8c2e07-0639-4720-bdcd-964442c81f72  rack1
UN  10.244.1.156  96.22 KiB  1       63.1%             cfa16fe3-43c0-4c3c-96a6-474801436346  rack1
```
`DM` = DOWN + still MOVING. The graceful shutdown replaced MOVING with SHUTDOWN in gossip (see (a)) but did
NOT clear MOVING from TokenMetadata. In a FIXED build (>=4.0.1) markAsShutdown() fires onChange and the node
would read `DN` (Down/Normal), with pending ranges cleared. `DM`-after-`shutdown,true` IS the buggy state.

### (c) cass-0 system.log: cass-2 only marked DOWN, never "removed"/"state normal" -> moving status sticks
```
INFO  [GossipStage:1] 2026-06-12 09:27:52,603 Gossiper.java:1286 - InetAddress /10.244.2.132:7000 is now DOWN
```

## Control (A/B reasoning)
The fix commit (fbb20b9...) only adds the missing `subscriber.onChange(...)` in `Gossiper.markAsShutdown()`.
In the fixed image (4.0.1) the same MOVE + graceful-shutdown sequence delivers the SHUTDOWN STATUS change to
TokenMetadata, which clears the node's MOVING status and pending ranges; the peer would show `DN`, not `DM`,
and the inflated replica set disappears. (Within-version control reasoning; client-symptom sweep below.)

## CLIENT-VISIBLE SYMPTOM — bogus UnavailableException (the Jira's named symptom)

With cass-2 stuck `DM` (shutdown but still MOVING) and BOTH peers (cass-0, cass-1) `UN`, a QUORUM write
sweep over all 30 keys (RF=2, so QUORUM=2) coordinated from cass-0:
`for k in 1..30: cqlsh -e "CONSISTENCY QUORUM; INSERT INTO repro16796.t (id,v) VALUES ($k,'sweep');"`

```
id=1  => required_replicas': 2, 'alive_replicas': 1     <- LEGITIMATE (cass-2 is a natural replica of id=1, it is down)
id=21 => required_replicas': 3, 'alive_replicas': 2     <- *** BOGUS *** impossible for RF=2
id=28 => required_replicas': 2, 'alive_replicas': 1     <- LEGITIMATE (cass-2 natural replica, down)
```

Full server line for the bogus id=21 failure:
```
<stdin>:1:NoHostAvailable: ('Unable to complete the operation against any hosts', {<Host: 127.0.0.1:9042 dc1>: Unavailable('Error from server: code=1000 [Unavailable exception] message="Cannot achieve consistency level QUORUM" info={'consistency': 'QUORUM', 'required_replicas': 3, 'alive_replicas': 2}')})
```

### Why id=21 is the smoking gun (self-proving, no control needed)
- `nodetool getendpoints repro16796 t 21` -> natural replicas = {10.244.1.156 (cass-0, UP), 10.244.2.132 (cass-2, DOWN)}.
- RF=2 => a QUORUM write needs only 2 replicas, and can never need 3.
- `required_replicas: 3` means the coordinator added a PHANTOM PENDING replica (cass-1) to the write set,
  inflating blockForWrite 2->3. A pending replica exists ONLY because cass-2's MOVING status is still in
  TokenMetadata while cass-2 is SHUTDOWN — i.e. CASSANDRA-16796 exactly. `alive_replicas: 2` shows it failed
  even though 2 nodes are up; the inflation to 3 is impossible under correct RF=2 behavior => BOGUS unavailable.
- Contrast: id=1/id=28 show `required:2/alive:1` — correct behavior (cass-2 is a *natural* replica there and is
  down), confirming `required:3` is the discriminating impossible-for-RF=2 signature.

### Reproducibility note (honest)
The bogus client symptom is RACE-Y / TRANSIENT, exactly as the Jira states ("peers can *sometimes* maintain
pending ranges ... in practice until the peer is next bounced"). The `required:3/alive:2` line above was
emitted verbatim by the server during the first QUORUM sweep. Subsequent sweeps succeeded because an
intervening gossip event (a disablegossip/enablegossip I did on cass-1) triggered a PendingRangeCalculator
rerun that recomputed that particular range mapping away. The PERSISTENT, deterministic part of the bug is the
`DM` (Down + Moving) TokenMetadata state after a graceful `shutdown,true` — that never cleared on its own
(evidence (a)/(b)/(c) above), which is the root cause; the bogus-unavailable client symptom is its
intermittent downstream consequence.

