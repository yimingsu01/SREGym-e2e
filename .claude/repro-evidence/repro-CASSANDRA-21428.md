# CASSANDRA-21428 — Nodes can become stuck in DOWN if ECHO_REQ has timeout

- **Buggy version:** 4.0.20 (distributed-multinode, multi-node)
- **Fix versions:** 4.0.21 / 4.1.12 / 5.0.9 / 6.0 — fix patch (4.0.21) is ABOVE the released
  Docker Hub ceiling for the 4.0 series (4.0.20). **No fixed-version control image exists.**
- **Component:** Cluster/Gossip
- **Disposition:** confirmed-blocked (structural — pre-deploy; partition primitive cannot be staged)
- **Namespace repro-21428:** NOT created. The blocker is pre-deploy (the message-drop primitive,
  not the ring), so no ring was deployed; nothing to tear down.

## 1. Primary source (JIRA fields.description) — exact root cause

> In Gossiper, echoHandler only implements onResponse. RequestCallback.onFailure has a default
> no-op, so when the ECHO_REQ times out or the remote node returns an error,
> `inflightEcho.remove(addr)` is never called. The stale entry persists. Any subsequent
> `markAlive(addr, localState)` call — where localState is the same in-place-mutated object
> already in inflightEcho — sees `localState.equals(prevState) = true` (identity equality, same
> reference) and skips indefinitely. In a temporary-partition scenario (node briefly unreachable,
> echo times out, node recovers with the same generation), the node can get stuck permanently
> dead: the failure detector sees it as alive and keeps triggering markAlive, but every invocation
> is suppressed by the stale entry. The stale entry is only cleared by `removeEndpoint()` (explicit
> removal) or `silentlyMarkDead()` via `markDead()` (failure detector conviction) — neither of
> which fires if the failure detector is reporting the node as healthy.
>
> Fix: override onFailure in echoHandler to call `inflightEcho.remove(addr)`.

Fix diff (src/java/org/apache/cassandra/gms/Gossiper.java): converts the `echoHandler` lambda into
a full `RequestCallback` that overrides `invokeOnFailure()`→true and
`onFailure(from, failureReason)`→`inflightEcho.remove(addr)`.

### Exact preconditions the bug requires (all must hold simultaneously)
1. ECHO_REQ from node A to peer B must hit the **onFailure / timeout** path (request expiry or
   remote error) — NOT a clean onResponse.
2. B must recover with the **same generation** (no process restart; restart bumps generation and
   takes a different code path).
3. The **failure detector on A must still consider B healthy** — i.e. B must NOT be convicted,
   because conviction calls `markDead → silentlyMarkDead` which clears the stale `inflightEcho`
   entry (the escape hatch). Only with the entry left stale does the subsequent `markAlive` get
   suppressed forever by identity-equality (`localState == prevState`).

The required state is therefore: **"ECHO_REQ failed" AND "FD reports peer healthy"** at the same
time — an asymmetric condition on a single peer.

## 2. Environment facts verified against the actual buggy image (cassandra:4.0.20)

Inspected the pre-existing read-only `cass-4-0-20/cass` pod (NOT mutated; tc rule added and
immediately deleted to probe capability only).

### 2a. Tooling / capability inventory (the partition primitive)
```
$ kubectl exec -n cass-4-0-20 cass -- sh -c 'which iptables tc nc nft; id; cat /proc/1/status | grep Cap'
/usr/sbin/tc                 # only tc present; NO iptables, NO nft, NO nc in the image
uid=0(root) gid=0(root) groups=0(root)
CapInh: 0000000000000000
CapPrm: 0000000000000000
CapEff: 0000000000000000     # ZERO effective capabilities
CapBnd: 00000000a80425fb
```
Capability bitmask decode (CapBnd = 0xa80425fb):
```
CAP_NET_ADMIN (12)  NOT in bounding set
CAP_NET_RAW   (13)  in bounding set
CAP_SYS_ADMIN (21)  NOT in bounding set
CapEff = 0 => no effective caps regardless of bounding set
```
Empirical tc test (the VERBATIM denial — corroborating, not the headline blocker):
```
$ kubectl exec -n cass-4-0-20 cass -- sh -c 'tc qdisc add dev eth0 root netem loss 100%; echo exit=$?'
RTNETLINK answers: Operation not permitted
exit=2
```
NOTE: this capability gap is *circumventable* — I could add
`securityContext.capabilities.add: ["NET_ADMIN"]` to my OWN pod manifest in repro-21428 (that is
not editing repo/tooling/Cassandra). So the capability gap is NOT the decisive blocker. It is
listed only as color. The decisive blocker is in §3.

### 2b. Messaging topology + timing (grounds the §3 argument)
```
$ kubectl exec -n cass-4-0-20 cass -- grep -E '^storage_port|^ssl_storage_port' /etc/cassandra/cassandra.yaml
storage_port: 7000
ssl_storage_port: 7001
$ ss -tlnp | grep 7000
LISTEN 0 512 10.244.3.6:7000 0.0.0.0:*        # single internode storage listener
$ grep -E 'request_timeout_in_ms|phi_convict' /etc/cassandra/cassandra.yaml
request_timeout_in_ms: 10000                  # ECHO_REQ expiry window ~10s
# phi_convict_threshold: 8  (commented => default 8)
$ nodetool version  ->  4.0.20                # confirmed buggy version
```

## 3. DECISIVE BLOCKER — message multiplexing on a single internode connection

ECHO_REQ and gossip SYN/heartbeat messages are BOTH internode MessagingService verbs and BOTH
travel over the SAME internode TCP connection between the host pair, on the storage port (7000,
verified above as the single internode listener). All internode verbs are multiplexed onto the
MessagingService connections on the storage port; there is no separate transport for ECHO_REQ.

Consequence: any partition that `tc`/`iptables`/connection-level tooling can stage is
**connection-level** — it drops ECHO_REQ and gossip heartbeats *together*.

- To make the ECHO_REQ **expire** you must hold the connection down for ≥ `request_timeout_in_ms`
  = ~10s.
- During those same ~10s, gossip heartbeats over the same TCP/7000 are also starved. With gossip
  interval 1s and `phi_convict_threshold` = 8 (default), the phi-accrual failure detector convicts
  the peer well within a ~10s outage.
- Conviction fires `markDead → silentlyMarkDead`, which **clears** the stale `inflightEcho` entry —
  the EXACT escape hatch the JIRA says must NOT fire for the bug to manifest.

Therefore the state the bug needs — **"ECHO_REQ failed" AND "FD still healthy"** — is not a narrow
timing window reachable with longer/shorter blanket partitions; it is **structurally unreachable**
with connection-level partitioning, because the two conditions are coupled through one TCP
connection and move in opposite directions as outage duration grows. A blanket-loss coin-flip would
not, in any case, yield a deterministic verbatim signature.

### What WOULD be required (the un-stageable mechanism)
An **asymmetric, verb-level message drop**: drop the `ECHO_REQ` verb while continuing to deliver
gossip SYN/ACK/heartbeat verbs on the same connection. That requires either:
- in-JVM multi-node dtest message filters (`IMessageFilters` verb drop / `org.apache.cassandra.
  distributed` test harness), or
- a protocol-aware internode proxy that parses the MessagingService framing and selectively drops
  the ECHO_REQ verb.

Neither can be staged with `kubectl exec` + stock `cassandra:4.0.20` pods inside kind. This is
precisely the "in-JVM multi-node dtest internals" case named in the confirmed-blocked criteria.
Matches the prior assessment (timing-sensitive / blocked-hard), now grounded empirically.

## 4. Control
No fixed image exists (fix patch 4.0.21 > released ceiling 4.0.20), so no A/B control was possible.
Within-version reasoning: the fix purely adds `onFailure → inflightEcho.remove(addr)`; with no way
to drive the buggy onFailure path deterministically (per §3) there is no observable buggy output to
contrast against.

## 5. Verbatim buggy signature
NONE obtained. The buggy code path (echoHandler.onFailure no-op leaving a stale inflightEcho entry
while FD stays healthy) cannot be driven from kubectl-level connection partitioning. Per the
evidence bar, with no verbatim signature this is **confirmed-blocked**, not reproduced.

## 6. Tooling findings
None. SREGym tooling was not exercised for an injection (pre-deploy block). No repo/tooling/
Cassandra files edited. No namespace created.
