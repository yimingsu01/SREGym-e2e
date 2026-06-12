# CASSANDRA-16718 Reproduction — Evidence Log

- **Issue**: CASSANDRA-16718 — "Changing listen_address with prefer_local may lead to issues"
- **Buggy image**: cassandra:4.1.1   |   **Fixed control**: cassandra:4.1.2  (fixVersions: 4.0.10, 4.1.2, 5.0-alpha1, 5.0)
- **Namespace**: repro-16718 (created by me)  |  **Topology**: 2 plain pods on kind-worker3 + per-pod ClusterIP services (stable broadcast_address, changing listen_address). No PVC.
- **Disposition**: **reproduced** (verbatim shadow-round failure on 4.1.1; fixed image's shadow round succeeds under identical steps)
- **Date**: 2026-06-12

---

## 1. Primary source (Jira body — ground truth)

> if prefer_local is enabled, I observed that nodes were unable to join the cluster and fail with
> 'Unable to gossip with any seeds'. Trace shows that the changing node will try to communicate with the
> existing node but the response is never received. I assume it is because the existing node attempts to
> communicate with the local address during the shadow round.

Components: Local/Config.

**Mechanism** (verified via fix commit b791644fda91b343a679bb0c2c1e33e594524636 on GitHub):
With a reconnectable snitch (prefer_local=true), Cassandra caches each peer's INTERNAL_ADDRESS_AND_PORT
as `preferred_ip` in system.peers and routes outbound gossip there (OutboundConnectionSettings used
`SystemKeyspace.getPreferredIP`). When a node keeps a stable broadcast/gossip identity but its internal
(listen) address changes, the seed still routes to the OLD internal address; the shadow-round reply never
reaches the node at its new address -> `doShadowRound` throws. Fix: `ReconnectableSnitchHelper.onDead()`
purges the stale INTERNAL_ADDRESS_AND_PORT and closes the outbound connection; OutboundConnectionSettings
now resolves via `Gossiper.getInternalAddressAndPort`.

**Why a per-pod ClusterIP is the correct staging**: the reporter's "container based solutions" have a
STABLE broadcast_address and a CHANGING listen_address. In kind, a ClusterIP service gives a stable IP
that survives pod recreation -> use it as `CASSANDRA_BROADCAST_ADDRESS`; leave listen_address = pod IP
(auto). That yields internal (listen=pod IP) != broadcast (ClusterIP) and makes prefer_local's
`preferred_ip` non-null (= the pod IP) — the exact pointer the bug corrupts. (A first attempt with a
flat StatefulSet+PVC, where listen==broadcast, gave preferred_ip=null and rejoined cleanly — that
topology cannot exercise the bug. This per-pod-ClusterIP topology does.)

---

## 2. Setup

Images on kind-worker3 (host `kind load` hit a multi-arch ctr digest bug; used `crictl pull` directly):
```
docker exec kind-worker3 crictl pull cassandra:4.1.1   # exit 0
docker exec kind-worker3 crictl pull cassandra:4.1.2   # exit 0
```
2 ClusterIP services (svc-0 seed, svc-1) + 2 plain pods, each with:
`cassandra-rackdc.properties` = {dc=dc1, rack=rack1, **prefer_local=true**}, snitch=GossipingPropertyFileSnitch,
`CASSANDRA_BROADCAST_ADDRESS` = its own service ClusterIP, listen_address auto = pod IP, seeds = svc-0 ClusterIP.

---

## 3. BUGGY RUN — cassandra:4.1.1

Ring formed UN/UN (broadcast = ClusterIPs):
```
$ kubectl exec -n repro-16718 cass-0 -- nodetool status
UN  10.96.227.137  133.18 KiB  16  100.0%  4be25a33-13e5-472d-becf-1cfa9ae71b3f  rack1   <- cass-1 (svc-1 ClusterIP)
UN  10.96.195.147  119.29 KiB  16  100.0%  6f2685b3-ffe9-4a77-bcf1-4c0fc93a0b7b  rack1   <- cass-0 seed
```
Precondition satisfied (internal != broadcast; preferred_ip non-null):
```
$ kubectl exec -n repro-16718 cass-1 -- cqlsh -e "SELECT broadcast_address, listen_address FROM system.local"
 broadcast_address | listen_address
     10.96.227.137 |   10.244.1.158          <- broadcast=ClusterIP, listen=pod IP

$ kubectl exec -n repro-16718 cass-0 -- cqlsh -e "SELECT peer, preferred_ip FROM system.peers"
 peer          | preferred_ip
 10.96.227.137 | 10.244.1.158                <- prefer_local cached cass-1's POD IP as preferred route
```
TRIGGER — delete cass-1, recreate (NEW pod IP 10.244.1.159, SAME broadcast ClusterIP 10.96.227.137).
Seed retained the STALE preferred_ip = old pod IP 10.244.1.158:
```
$ (right after delete) SELECT peer, preferred_ip FROM system.peers
 10.96.227.137 | 10.244.1.158                <- STALE (cass-1 is now at 10.244.1.159; .158 is dead)
```
RESULT — cass-1 went into CrashLoopBackOff. Seed shows it DN (never rejoined):
```
$ kubectl exec -n repro-16718 cass-0 -- nodetool status
DN  10.96.227.137  133.18 KiB  16  100.0%  4be25a33-...  rack1
UN  10.96.195.147  119.29 KiB  16  100.0%  6f2685b3-...  rack1
```

### VERBATIM BUGGY SIGNATURE (cass-1 4.1.1, `kubectl logs --previous`):
```
Exception (java.lang.RuntimeException) encountered during startup: Unable to gossip with any peers
java.lang.RuntimeException: Unable to gossip with any peers
	at org.apache.cassandra.gms.Gossiper.doShadowRound(Gossiper.java:1916)
	at org.apache.cassandra.service.StorageService.checkForEndpointCollision(StorageService.java:694)
	at org.apache.cassandra.service.StorageService.prepareToJoin(StorageService.java:996)
	at org.apache.cassandra.service.StorageService.initServer(StorageService.java:842)
	at org.apache.cassandra.service.StorageService.initServer(StorageService.java:775)
	at org.apache.cassandra.service.CassandraDaemon.setup(CassandraDaemon.java:425)
	at org.apache.cassandra.service.CassandraDaemon.activate(CassandraDaemon.java:752)
	at org.apache.cassandra.service.CassandraDaemon.main(CassandraDaemon.java:876)
```
Corroborating "response never received" (Jira symptom): the OUTBOUND connection from cass-1's NEW pod IP
to the seed succeeded, but the shadow round still timed out (reply mis-routed to the dead old internal IP):
```
INFO [Messaging-EventLoop-3-1] OutboundConnection.java:1153 -
  /10.96.227.137:7000(/10.244.1.159:45234)->/10.96.195.147:7000-SMALL_MESSAGES ... successfully connected
```
restartCount climbed to 4+; DN persisted. This is CASSANDRA-16718: `doShadowRound` itself throws.

---

## 4. A/B CONTROL — cassandra:4.1.2 (fixed), IDENTICAL steps

Fresh services (svc-0=10.96.121.172, svc-1=10.96.222.151), same manifest, image 4.1.2.
Ring formed UN/UN; SAME precondition reproduced:
```
$ SELECT broadcast_address, listen_address FROM system.local   (cass-1)
 10.96.222.151 |   10.244.1.161          <- internal != broadcast, identical setup
$ SELECT peer, preferred_ip FROM system.peers   (seed)
 10.96.222.151 | 10.244.1.161            <- preferred_ip non-null (pod IP), identical
```
TRIGGER — delete cass-1, recreate (NEW pod IP 10.244.1.162, SAME broadcast 10.96.222.151).
Seed retained STALE preferred_ip = old pod IP 10.244.1.161 (identical stale-pointer condition):
```
$ (right after delete) SELECT peer, preferred_ip FROM system.peers
 10.96.222.151 | 10.244.1.161
```

### FIXED-VERSION OUTCOME (cass-1 4.1.2, `kubectl logs --previous`):
```
Exception (java.lang.RuntimeException) encountered during startup:
  A node with address /10.96.222.151:7000 already exists, cancelling join.
  Use cassandra.replace_address if you want to replace this node.
java.lang.RuntimeException: A node with address /10.96.222.151:7000 already exists, cancelling join. ...
	at org.apache.cassandra.service.StorageService.checkForEndpointCollision(StorageService.java:784)
	at org.apache.cassandra.service.StorageService.prepareToJoin(StorageService.java:1075)
	at org.apache.cassandra.service.StorageService.initServer(StorageService.java:921)
	...
```

### THE DISCRIMINATOR (why this proves the bug, not plumbing)
Both versions call `StorageService.checkForEndpointCollision`, which internally calls
`Gossiper.doShadowRound`:
- **4.1.1 (buggy)**: dies INSIDE the shadow round -> `Gossiper.doShadowRound(Gossiper.java:1916)` ->
  "Unable to gossip with any peers". The seed's reply was mis-routed to the stale internal address, so
  the shadow round NEVER COMPLETED. This is exactly the reported failure path.
- **4.1.2 (fixed)**: `doShadowRound` SUCCEEDS (the fix purges the stale internal address / resolves the
  current one, so the reply reaches the new pod IP). Startup then proceeds PAST the shadow round to a
  DIFFERENT, EXPECTED guard at `StorageService.java:784` — "A node with address ... already exists" —
  which is correct behavior (I deliberately reuse the same broadcast endpoint without replace_address).

So under byte-for-byte identical topology, config, and trigger, the buggy version fails IN the gossip
shadow round (the bug) while the fixed version's shadow round WORKS and it only stops later at a normal
collision check. The differing exception class + differing stack frame at the same call site is the
A/B proof that 4.1.2 fixed the gossip mis-routing while 4.1.1 exhibits it.

(The collision guard on 4.1.2 is an artifact of my reuse-same-endpoint trigger, not a regression; it is
the standard "use replace_address" protection and is independent of CASSANDRA-16718. The load-bearing
contrast is doShadowRound throwing on 4.1.1 vs returning on 4.1.2.)

---

## 5. Disposition

**reproduced.** Verbatim buggy signature obtained on the buggy image 4.1.1:
`java.lang.RuntimeException: Unable to gossip with any peers  at org.apache.cassandra.gms.Gossiper.doShadowRound(Gossiper.java:1916)`
thrown during the gossip shadow round when prefer_local routed the seed's reply to cass-1's stale internal
(pod) address after listen_address changed but broadcast stayed stable. The fixed image (4.1.2) completes
the shadow round under identical steps (fails later only at the unrelated endpoint-collision guard).

**tag_correction**: classifier hint trigger ("enable prefer_local + change listen_address + restart ->
'Unable to gossip with any seeds'") is accurate to the Jira body and DID reproduce — but only when the
node has a STABLE broadcast identity distinct from its changing listen address (staged via a per-pod
ClusterIP). A naive flat StatefulSet (listen==broadcast, preferred_ip=null) does NOT reproduce it; that
is a staging requirement, not a topology-tag error. Also: the buggy build's exact message is "Unable to
gossip with any **peers**" (the Jira reporter paraphrased "seeds"); same code path (Gossiper.doShadowRound).

**tooling_findings**: `kind load docker-image cassandra:4.1.1/4.1.2` failed with
`ctr: content digest sha256:... not found` (multi-arch manifest import incompatibility in this kind/ctr).
Worked around with `docker exec <node> crictl pull <image>`. (Pre-existing repro-15899/cass-0 in
ErrImageNeverPull is another session's namespace; not touched.)

---

## 6. Teardown
`kubectl delete ns repro-16718 --wait=false` (cascades pods + services). See structured result.
