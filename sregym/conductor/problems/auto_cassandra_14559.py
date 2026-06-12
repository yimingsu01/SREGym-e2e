"""CASSANDRA-14559: Check for endpoint collision with hibernating nodes.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-14559
Buggy: 3.11.7  ->  Fixed: 3.0.22, 3.11.8, 4.0-beta2, 4.0
Components: Consistency / Bootstrap and Decommission

STUB: multi-node reproduction not yet encoded as a single-cluster Problem — see steps below.

Reproduction summary (2-node ring; needs a surviving peer that does the FatClient
conviction plus the node being replaced at a STABLE same address):
  A node is replaced via `-Dcassandra.replace_address=<its own IP>` (the same-address
  HIBERNATE path), killed mid-bootstrap so a `STATUS:hibernate,true` gossip entry is left
  on the surviving peer, then wiped and restarted WITHOUT the replace_address flag. On
  3.11.7 this no-flag restart is ALLOWED (no collision check) and begins a fresh bootstrap
  with new tokens; killing it mid-bootstrap a second time makes the surviving peer convict
  it as a FatClient ~30s later, unsafely removing the endpoint AND its tokens from gossip.
  The 3.11.8 fix adds a hibernate-collision check in checkForEndpointCollision that REFUSES
  the no-flag restart ("A node with address ... already exists, cancelling join").

Verbatim buggy signature (literal copy from the surviving peer's system.log):
  INFO  [GossipTasks:1] 2026-06-12 08:17:07,912 Gossiper.java:880 - FatClient /10.244.1.141 has been silent for 30000ms, removing from gossip
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra14559(GenericCustomBuildProblem):
    # STUB: multi-node ring reproduction. The seed/target two-pod topology, in-pod
    # process restarts, replace_address manipulation, and the surviving peer's ~30s
    # FatClient conviction CANNOT be expressed as a single GenericCustomBuildProblem
    # CQL reproducer. Encoded here as an honest stub: db metadata + root cause are set
    # and the full multi-node steps live in `reproducer` as a TODO. Do NOT flatten this
    # into one CQL — that would compile and register but silently NOT reproduce the bug.
    db_name = "cassandra"
    db_version = "3.11.7"
    source_git_ref = "cassandra-3.11.7"
    # 3.11.7 is the released fix patch minus one, so the stock image already ships the bug.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/service/StorageService.java"
    root_cause_description = (
        "checkForEndpointCollision() does not reject a node that restarts WITHOUT "
        "cassandra.replace_address when a gossip entry for the same broadcast address is "
        "already in the HIBERNATE state (left behind by an interrupted same-address replace). "
        "On 3.11.7 such a no-flag restart is allowed and begins a fresh bootstrap with new "
        "tokens; if it is killed mid-bootstrap, the surviving peer convicts it as a FatClient "
        "~30s later and unsafely removes the endpoint and its tokens from gossip. The 3.11.8 "
        "fix adds a hibernate-collision check (Gossiper.java:825 warning) that throws a "
        "RuntimeException 'A node with address ... already exists, cancelling join. Use "
        "cassandra.replace_address if you want to replace this node.' to block the unsafe path."
    )

    # NOTE: prose steps, NOT CQL — this is a multi-node ring orchestration, not a query.
    # Topology: 2-node ring in one namespace.
    #   - pod `seed`   (cassandra:3.11.7) — surviving peer, stays UP, does the conviction.
    #   - pod `target` (cassandra:3.11.7) — the node being replaced; its container command is
    #     `tail -f /dev/null` so cassandra runs as a launched/killed PROCESS via the image
    #     entrypoint while the POD is never deleted, keeping its IP (e.g. 10.244.1.141) STABLE
    #     across in-pod restarts — that stable IP IS the "replace with the same address"
    #     precondition. `-Dcassandra.ring_delay_ms=60000` is set on the TARGET launches only
    #     (a runtime JVM property, not a source edit) to widen the mid-bootstrap kill window;
    #     the seed's FatClient timer stays at its default ~30s.
    reproducer = """
STUB: multi-node reproduction not yet encoded as a single-cluster Problem — see steps below.

Two-node ring, ns=repro-14559: pod `seed` and pod `target`, both cassandra:3.11.7. The
`target` pod's container command is `tail -f /dev/null`; cassandra is started/killed inside
it as a process via docker-entrypoint.sh, and the pod is never deleted so target's IP stays
stable (the same-address precondition). `-Dcassandra.ring_delay_ms=60000` is passed on every
target launch (runtime JVM property) to widen the mid-bootstrap kill window.

1. Start cassandra on `target` so it joins normally and the ring shows both nodes UN
   (UP/Normal). Launch:
     kubectl exec target -- bash -lc 'export JVM_EXTRA_OPTS="-Dcassandra.ring_delay_ms=60000"; setsid nohup docker-entrypoint.sh cassandra -f >/tmp/c.log 2>&1 &'

2. Kill cassandra on `target`: `kubectl exec target -- pkill -9 -f CassandraDaemon`.
   Wait until `nodetool status` on `seed` shows target as DN (Down/Normal).

3. Wipe target's data + commitlog so it presents as a fresh node, then relaunch with
   replace_address set to TARGET'S OWN (same) IP — this enters the same-address HIBERNATE
   replace path ("Writes will not be forwarded to this node during replacement because it has
   the same address as the node to be replaced"):
     rm -rf /var/lib/cassandra/{data,commitlog,hints,saved_caches}
     JVM_EXTRA_OPTS="-Dcassandra.ring_delay_ms=60000 -Dcassandra.replace_address=<TARGET_IP>"
     setsid nohup docker-entrypoint.sh cassandra -f >/tmp/cR.log 2>&1 &
   Wait until target logs "JOINING: calculation complete, ready to bootstrap".

4. Kill cassandra on `target` mid-bootstrap, BEFORE it reaches NORMAL:
   `kubectl exec target -- pkill -9 -f CassandraDaemon`.
   The seed now holds a HIBERNATE gossip entry for target's endpoint
   (STATUS:hibernate,true) — this is the state the 3.11.8 fix guards against.

5. Wipe target's data dir again and relaunch WITHOUT the replace_address flag (THE UNSAFE
   PATH on 3.11.7). On 3.11.7 the node is ALLOWED to start and begins a FRESH bootstrap with
   NEW tokens (no collision refusal); the seed's view flips to UJ with a new Host ID:
     rm -rf /var/lib/cassandra/{data,commitlog,hints,saved_caches}
     JVM_EXTRA_OPTS="-Dcassandra.ring_delay_ms=60000"
     setsid nohup docker-entrypoint.sh cassandra -f >/tmp/cS.log 2>&1 &
   (On 3.11.8 this step is REFUSED with a RuntimeException at
   checkForEndpointCollision: "A node with address /<TARGET_IP> already exists, cancelling
   join. Use cassandra.replace_address if you want to replace this node.")

6. Kill cassandra on `target` mid-bootstrap AGAIN, while it is in the 60s pending-range
   sleep: `kubectl exec target -- pkill -9 -f CassandraDaemon`.

7. ~30s later the seed convicts the endpoint as a FatClient and removes it — and its tokens —
   from gossip. Seed system.log:
     Gossiper.java:1106 - InetAddress /<TARGET_IP> is now DOWN
     Gossiper.java:880  - FatClient /<TARGET_IP> has been silent for 30000ms, removing from gossip
   Confirm: `nodetool status` on seed shows ONLY the seed; `nodetool gossipinfo` no longer
   lists target's endpoint. The node and its tokens were UNSAFELY removed from gossip.
"""

    # Multi-node stub: the prose `reproducer` above is NOT executable as a single-cluster CQL
    # loop, so do not attach a continuous-reproducer pod / mitigation oracle (it would run the
    # non-executable steps and yield a meaningless Ready/NotReady). The diagnosis LLM-judge
    # oracle on root_cause_description still applies.
    continuous_reproducer = False
