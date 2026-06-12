# CASSANDRA-14559 Reproduction Evidence

**Summary (Jira):** Check for endpoint collision with hibernating nodes
**Components:** Consistency/Bootstrap and Decommission
**fixVersions:** 3.0.22, 3.11.8, 4.0-beta2, 4.0
**Buggy image:** cassandra:3.11.7   **Fixed-control image:** cassandra:3.11.8 (<= 3.11 ceiling 19)
**Disposition: REPRODUCED**

## Classifier hint vs reality (tag_correction)
Hint: topology=ring, confidence=M, trigger="replace_address same IP, stop mid-bootstrap, restart
without flag, stop again -> node removed from gossip after 30s (FatClient)".
The hint matched the Jira body EXACTLY. topology=ring is correct (needs >=2 nodes: a surviving
peer to do the FatClient conviction + the replaced node). No correction needed.

## Reproducer extracted from the Jira description (ground truth)
1. Create N-node cluster.
2. Stop a node.
3. Replace the stopped node with a node using the SAME address via the `replace_address` flag.
4. Stop the node before it finishes bootstrapping.
5. Remove the `replace_address` flag and restart to resume bootstrapping (clearing the data dir
   also makes it generate new tokens).
6. Stop the node before it finishes bootstrapping again.
7. ~30s later the node is removed from gossip because it now matches the FatClient check ->
   the node AND ITS TOKENS are unsafely removed from gossip.

The Jira-proposed fix: prevent a non-bootstrapped node (without replace_address) from starting if
there is a gossip entry for the same address in the HIBERNATE state.

## Topology used (inside existing kind cluster, ns=repro-14559)
- pod `seed`  (cassandra:3.11.7) -- surviving peer, stays UP the whole time, does the conviction.
- pod `target` (cassandra:3.11.7) -- the node being replaced. Its container `command` is
  `tail -f /dev/null` (a sleeper) so cassandra is launched/killed as a PROCESS via the image
  entrypoint. The POD is never deleted => its IP (10.244.1.141) is stable across process restarts,
  which IS the "replace with the SAME address" precondition. Steps 2-6 are in-pod process
  restarts (`pkill -9 -f CassandraDaemon` / relaunch with controlled JVM_EXTRA_OPTS).
- `-Dcassandra.ring_delay_ms=60000` set on the TARGET launches only, to widen the "mid-bootstrap"
  kill window. This is a runtime JVM property (NOT a source edit) -> within RECORD-ONLY. The
  seed's FatClient timer stays at its default RING_DELAY ~= 30s (the "30 seconds later" in report).

Seed IP=10.244.2.118 ; Target IP=10.244.1.141.

================================================================================
## BUGGY RUN (cassandra:3.11.7)
================================================================================

### Step 1 - target joins normally -> UN (2-node ring)
`kubectl exec target -- bash -lc 'export JVM_EXTRA_OPTS="-Dcassandra.ring_delay_ms=60000"; setsid nohup docker-entrypoint.sh cassandra -f >/tmp/c.log 2>&1 &'`
nodetool status on seed:
    UN  10.244.2.118  74.97 KiB  256  100.0%  16e8a886-...  rack1
    UN  10.244.1.141  84.25 KiB  256  100.0%  9ae376f2-9ddc-4ad9-89e7-4f6155614adb  rack1

### Step 2 - kill cassandra in target; seed marks it DN
`kubectl exec target -- pkill -9 -f CassandraDaemon`
    DN  10.244.1.141  84.25 KiB  256  100.0%  9ae376f2-...  rack1

### Step 3 - replace_address with the SAME IP (after clearing data dir so it is a fresh node)
Data dir cleared (`rm -rf /var/lib/cassandra/{data,commitlog,hints,saved_caches}`), then launched
with `-Dcassandra.replace_address=10.244.1.141`. /tmp/cR.log:
    StorageService.java:547 - Gathering node replacement information for /10.244.1.141
    StorageService.java:835 - Writes will not be forwarded to this node during replacement because
      it has the same address as the node to be replaced (/10.244.1.141). ...   <-- HIBERNATE path
    StorageService.java:1478 - JOINING: calculation complete, ready to bootstrap

### Step 4 - kill mid-bootstrap (before NORMAL)
`kubectl exec target -- pkill -9 -f CassandraDaemon`  (rc=0, was alive)
Seed gossipinfo for the endpoint AFTER step 4 -- the HIBERNATE entry that the fix guards against:
    /10.244.1.141
      STATUS:2:hibernate,true
      HOST_ID:4:9ae376f2-9ddc-4ad9-89e7-4f6155614adb
      TOKENS:1:<hidden>

### Step 5 - clear data + restart WITHOUT replace_address  (THE UNSAFE PATH on 3.11.7)
`rm -rf` data dir, then launched with NO replace_address. /tmp/cS.log -- the node is ALLOWED to
start and begins a FRESH bootstrap with NEW tokens (NO collision refusal):
    BootStrapper.java:228 - Generated random tokens. tokens are [2201274899872698033, ...]
    StorageService.java:1478 - JOINING: sleeping 60000 ms for pending range setup
Seed view: endpoint flips to UJ with a NEW Host ID (b5f462ce-6546-4ae1-9906-ad8c0fbdfc32),
confirming the fresh bootstrap overwrote the hibernate gossip state.

### Step 6 - kill mid-bootstrap again  (T0 = 08:16:37)
`kubectl exec target -- pkill -9 -f CassandraDaemon`  (rc=0, was alive, in the 60s range sleep)

### Step 7 - ~30s later the seed convicts it as a FatClient and removes it from gossip
Seed /opt/cassandra/logs/system.log (chronological, endpoint /10.244.1.141):
    08:16:15  Gossiper.java:1126 - Node /10.244.1.141 has restarted, now UP
    08:16:20  StorageService.java:2254 - Node /10.244.1.141 state jump to bootstrap   <-- unsafe fresh bootstrap, no collision check
    08:16:56  Gossiper.java:1106 - InetAddress /10.244.1.141 is now DOWN
    08:17:07  Gossiper.java:880 - FatClient /10.244.1.141 has been silent for 30000ms, removing from gossip

#### >>> VERBATIM BUGGY SIGNATURE (literal copy from seed system.log) <<<
    INFO  [GossipTasks:1] 2026-06-12 08:17:07,912 Gossiper.java:880 - FatClient /10.244.1.141 has been silent for 30000ms, removing from gossip

### Confirmation the node + its tokens were UNSAFELY removed
After the FatClient line, on the seed:
- nodetool status: ONLY the seed remains; 10.244.1.141 is GONE
      UN  10.244.2.118  74.97 KiB  256  100.0%  16e8a886-...  rack1
- nodetool gossipinfo: `grep -c 10.244.1.141` => 0  (endpoint /10.244.1.141 NOT present)

This is precisely the bug: "the node (and its tokens) being unsafely removed from gossip."

================================================================================
## A/B CONTROL (cassandra:3.11.8 -- the FIXED version)
================================================================================
Identical dance in the same namespace with pods seed8/target8 (cassandra:3.11.8).
Seed8 IP=10.244.2.120 ; Target8 IP=10.244.1.143.

- Step 1: target8 joins UN.
- Step 2: killed, seed8 marks DN.
- Step 3: replace_address=10.244.1.143 -> SAME hibernate path:
    StorageService.java:841 - Writes will not be forwarded to this node during replacement because
      it has the same address as the node to be replaced (/10.244.1.143). ...
- Step 4: killed mid-bootstrap (hibernate entry established on seed8).
- Step 5: clear data + restart WITHOUT replace_address  -> **REFUSED TO START** (the new guard):

#### >>> FIXED-VERSION CONTROL SIGNATURE (literal copy from target8 /tmp/cS.log) <<<
    WARN  [main] 2026-06-12 08:20:43,059 Gossiper.java:825 - A node with the same IP in hibernate status was detected. Was a replacement already attempted?
    Exception (java.lang.RuntimeException) encountered during startup: A node with address /10.244.1.143 already exists, cancelling join. Use cassandra.replace_address if you want to replace this node.
    java.lang.RuntimeException: A node with address /10.244.1.143 already exists, cancelling join. Use cassandra.replace_address if you want to replace this node.
    	at org.apache.cassandra.service.StorageService.checkForEndpointCollision(StorageService.java:603) ~[apache-cassandra-3.11.8.jar:3.11.8]

Because the no-flag restart is rejected at checkForEndpointCollision (the new
"A node with the same IP in hibernate status was detected" check), the node NEVER enters a fresh
bootstrap. seed8 system.log shows NO `state jump to bootstrap` and NO `FatClient ... removing from
gossip` line. The unsafe removal is impossible on 3.11.8.

### Buggy vs Fixed contrast
| Step 5 (restart, no replace_address) | 3.11.7 (buggy) | 3.11.8 (fixed) |
|---|---|---|
| node start | ALLOWED -> fresh bootstrap, new tokens | REFUSED with RuntimeException at checkForEndpointCollision:603 |
| seed log  | `state jump to bootstrap` then `FatClient ... removing from gossip` | neither line appears |
| outcome   | endpoint + tokens unsafely removed from gossip | endpoint left untouched; operator told to use replace_address |

================================================================================
## Source corroboration (cassandra-3.11.7 StorageService.java)
- prepareForReplacement throws "Cannot replace address with a node that is already bootstrapped"
  when SystemKeyspace.bootstrapComplete() is true -> required clearing the data+commitlog dir so the
  replacing node presents as fresh (the report's "fresh node, same address" precondition).
- The same-address replace path sets ApplicationState.STATUS = hibernate(true) (the "Writes will not
  be forwarded..." warning), which is the hibernate gossip entry the fix later checks for.
- In 3.11.8 the fix adds, in checkForEndpointCollision, a rejection when an endpoint with the local
  broadcast address is in hibernate state and replace_address is not set (Gossiper.java:825 warning
  + the "A node with address ... already exists, cancelling join" RuntimeException at line 603).

## Notes / environment quirks (RECORD-ONLY; nothing fixed)
- `mesg: ttyname failed` lines from `bash -lc` over kubectl exec are harmless tty noise.
- `pkill -9 -f CassandraDaemon` returns rc=137 when it kills a process and rc=1 when none match;
  both are expected and were handled.
- All process kills were of cassandra inside the long-lived pod; pods were never deleted, so IPs
  stayed stable (essential for the same-address precondition).
