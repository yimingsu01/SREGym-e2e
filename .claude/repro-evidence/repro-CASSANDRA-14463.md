# CASSANDRA-14463 — Reproduction Evidence

**Summary:** Prevent the generation of new tokens when using replace_address flag
**Buggy version:** cassandra:4.0.0  | **Fixed-control:** cassandra:4.0.1 (A/B run)
**Components:** Legacy/Distributed Metadata | **fixVersions:** 3.0.25, 3.11.11, 4.0.1, 4.1-alpha1, 4.1
**Namespace:** repro-14463 (kind, context kind-kind) | **Disposition:** REPRODUCED

---

## 1. Bug mechanism (from Jira body + fix source)

The body: when an operator replaces a node with `replace_address` AND mistakenly lists that node in
its OWN seed list AND `initial_token` is unset, Cassandra (buggy) does NOT take over the dead node's
tokens — instead it skips streaming (because seeds don't bootstrap) and **generates a new set of random
tokens and joins the ring anyway**. The fix ("Don't allow seeds to replace without using unsafe",
CHANGES.txt 4.0.1) makes the node REFUSE to start in this configuration unless
`-Dcassandra.allow_unsafe_replace=true` is set.

### Discriminating code change — StorageService.prepareForReplacement()
Confirmed by fetching the source at both tags (raw.githubusercontent.com):

- 4.0.0 (BUGGY) guard:
  `if (!DatabaseDescriptor.isAutoBootstrap() && !Boolean.getBoolean("cassandra.allow_unsafe_replace")) throw ...`
  -> With default `auto_bootstrap=true`, `isAutoBootstrap()` is true, so the guard is NOT taken. The
  node proceeds, and because it is its own seed `shouldBootstrap()` returns false later -> skips
  streaming + generates new random tokens.
- 4.0.1 (FIXED) guard:
  `if (!shouldBootstrap() && !Boolean.getBoolean("cassandra.allow_unsafe_replace")) throw ...`
  where `shouldBootstrap() = isAutoBootstrap() && !bootstrapComplete() && !isSeed()`.
  -> A seed node has `isSeed()==true` => `shouldBootstrap()==false` => guard IS taken => refuses to start.

(The StorageService string table is byte-identical between 4.0.0 and 4.0.1; only the *condition* feeding
the existing "Replacing a node without bootstrapping..." RuntimeException changed.)

---

## 2. Reproducer (single-node, A/B)

The three preconditions are all satisfied by the SINGLE-POD template, no ring needed:
- **node in its own seed list**: the docker-entrypoint defaults `CASSANDRA_SEEDS` to the pod's own
  broadcast IP when unset (`: ${CASSANDRA_SEEDS:="$CASSANDRA_BROADCAST_ADDRESS"}`), so `isSeed()==true`.
- **initial_token unset, auto_bootstrap=true**: both defaults (verified in the Node configuration log).
- **replace_address set**: injected via `JVM_EXTRA_OPTS=-Dcassandra.replace_address=10.255.255.254`
  (the image appends JVM_EXTRA_OPTS; there is no dedicated replace env var). Setting replace_address is
  what *calls* prepareForReplacement(), so ring size is irrelevant to hitting the guard.

A dummy replace target IP (10.255.255.254, not in gossip) is used so the only thing under test is WHETHER
the seed+replace guard fires. (Using a real dead node's IP would change the downstream outcome but not the
guard decision, which is the bug.)

Both pods deployed with identical env; only the image tag differs.

### Commands
```
kubectl create namespace repro-14463
kubectl apply -f /tmp/repro-14463-buggy.yaml     # image cassandra:4.0.0
kubectl apply -f /tmp/repro-14463-control.yaml   # image cassandra:4.0.1
# each pod: env JVM_EXTRA_OPTS=-Dcassandra.replace_address=10.255.255.254, CASSANDRA_SEEDS unset
kubectl logs -n repro-14463 cass-buggy
kubectl logs -n repro-14463 cass-control
```

### Confirmation that preconditions held (identical in both pods' "Node configuration" log line)
```
auto_bootstrap=true; ... initial_token=null; ... num_tokens=16; ...
seed_provider=org.apache.cassandra.locator.SimpleSeedProvider{seeds=10.244.3.118}   # == own broadcast_address
```
JVM Arguments line (both pods): `... -Dcassandra.replace_address=10.255.255.254 ...`

---

## 3. VERBATIM buggy signature — cassandra:4.0.0 (passed the seed-replace guard)

Raw `kubectl logs -n repro-14463 cass-buggy` (lines 137-151):
```
INFO  [main] 2026-06-12 08:11:25,703 InboundConnectionInitiator.java:127 - Listening on address: (/10.244.3.118:7000), nic: eth0, encryption: unencrypted
WARN  [main] 2026-06-12 08:11:25,756 SystemKeyspace.java:1130 - No host ID found, created 0a537a9c-92bc-4b42-931b-5f8f62a1f6a1 (Note: This should happen exactly once per node).
INFO  [main] 2026-06-12 08:11:25,759 StorageService.java:528 - Gathering node replacement information for /10.255.255.254:7000
Exception (java.lang.RuntimeException) encountered during startup: Cannot replace_address /10.255.255.254:7000 because it doesn't exist in gossip
java.lang.RuntimeException: Cannot replace_address /10.255.255.254:7000 because it doesn't exist in gossip
	at org.apache.cassandra.service.StorageService.prepareForReplacement(StorageService.java:533)
	at org.apache.cassandra.service.StorageService.prepareToJoin(StorageService.java:911)
	at org.apache.cassandra.service.StorageService.initServer(StorageService.java:784)
	at org.apache.cassandra.service.StorageService.initServer(StorageService.java:729)
	at org.apache.cassandra.service.CassandraDaemon.setup(CassandraDaemon.java:420)
	at org.apache.cassandra.service.CassandraDaemon.activate(CassandraDaemon.java:763)
	at org.apache.cassandra.service.CassandraDaemon.main(CassandraDaemon.java:887)
```

**Key buggy signature line (most telling):**
```
StorageService.java:528 - Gathering node replacement information for /10.255.255.254:7000
```
This proves 4.0.0 PASSED the seed+replace guard and entered replacement logic. The downstream
RuntimeException at line **533** is only because the dummy target isn't in gossip — i.e. the guard at the
top of prepareForReplacement() did NOT block the seed+replace combo. Verified: the consistency-guarantee
guard string ("Replacing a node without bootstrapping...") appears **0 times** in the 4.0.0 log.

With a REAL dead node's IP (in gossip) this same un-blocked path is exactly what the Jira describes: the
seed skips streaming and generates a fresh random token set instead of inheriting the dead node's tokens.

---

## 4. A/B CONTROL — cassandra:4.0.1 (refuses at the guard, identical config)

Raw `kubectl logs -n repro-14463 cass-control` (lines 137-151):
```
INFO  [main] 2026-06-12 08:11:34,080 InboundConnectionInitiator.java:127 - Listening on address: (/10.244.3.119:7000), nic: eth0, encryption: unencrypted
WARN  [main] 2026-06-12 08:11:34,132 SystemKeyspace.java:1130 - No host ID found, created 9e03867d-65d1-461a-b228-89b1ca7d5b0d (Note: This should happen exactly once per node).
Exception (java.lang.RuntimeException) encountered during startup: Replacing a node without bootstrapping risks invalidating consistency guarantees as the expected data may not be present until repair is run. To perform this operation, please restart with -Dcassandra.allow_unsafe_replace=true
java.lang.RuntimeException: Replacing a node without bootstrapping risks invalidating consistency guarantees as the expected data may not be present until repair is run. To perform this operation, please restart with -Dcassandra.allow_unsafe_replace=true
	at org.apache.cassandra.service.StorageService.prepareForReplacement(StorageService.java:522)
	at org.apache.cassandra.service.StorageService.prepareToJoin(StorageService.java:911)
	at org.apache.cassandra.service.StorageService.initServer(StorageService.java:784)
	at org.apache.cassandra.service.StorageService.initServer(StorageService.java:729)
	at org.apache.cassandra.service.CassandraDaemon.setup(CassandraDaemon.java:420)
	at org.apache.cassandra.service.CassandraDaemon.activate(CassandraDaemon.java:763)
	at org.apache.cassandra.service.CassandraDaemon.main(CassandraDaemon.java:887)
```
Verified: "Gathering node replacement information" appears **0 times** in the 4.0.1 log — it never reaches
replacement logic; it refuses at prepareForReplacement() **line 522** with the allow_unsafe_replace
instruction.

### Side-by-side
| | 4.0.0 buggy | 4.0.1 fixed |
|---|---|---|
| "Replacing a node without bootstrapping..." guard | 0 occurrences (not blocked) | thrown @ StorageService:522 |
| "Gathering node replacement information" | YES @ StorageService:528 (proceeds) | 0 occurrences (blocked first) |
| Net behavior | enters replacement / would generate new tokens | refuses to start, demands -Dcassandra.allow_unsafe_replace=true |

The single variable changed between the two runs is the image tag; config (seeds=own IP,
auto_bootstrap=true, initial_token=null, replace_address=dummy) is byte-identical. => operator-visible
behavior difference attributable solely to the CASSANDRA-14463 fix.

---

## 5. Tag correction
HINT said topology=ring. Reality: **single-node is sufficient** — the bug lives in the per-node
prepareForReplacement() guard, gated only by (isSeed, auto_bootstrap, initial_token, replace_address),
all reproducible on one pod because the docker entrypoint makes a lone pod its own seed. No ring required.

## 6. Tooling note
None blocking. (`gh`, `java`/`javap` not on PATH on the host; worked around with raw.githubusercontent.com
via WebFetch and by reading CHANGES.txt out of the image.)

## 7. Artifacts
- /tmp/repro-14463-buggy-full.log   (full 4.0.0 startup log, 206 lines)
- /tmp/repro-14463-control-full.log (full 4.0.1 startup log, 205 lines)
- /tmp/repro-14463-buggy.yaml, /tmp/repro-14463-control.yaml (pod specs)

## 8. Teardown
`kubectl delete ns repro-14463 --wait=false`  (only namespace created by this session)
