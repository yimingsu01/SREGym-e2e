"""CASSANDRA-16718: Changing listen_address with prefer_local may lead to issues.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16718
Buggy: 4.1.1   ->   Fixed: 4.1.2  (also fixed in 4.0.10, 5.0-alpha1, 5.0)

STUB: multi-node reproduction not yet encoded as a single-cluster Problem — see steps below.

Reproduction summary (verified — see .claude/repro-evidence/repro-CASSANDRA-16718.md):
  On a UN/UN ring with prefer_local=true and a node whose internal (listen) address differs
  from its broadcast address, the seed caches the peer's internal (pod) IP as `preferred_ip`
  in system.peers and routes outbound gossip there. Delete cass-1 and recreate it so it returns
  with a NEW listen_address (new pod IP) but the SAME broadcast endpoint (stable ClusterIP):
  the seed retains the STALE preferred_ip (the old, dead internal IP), so the recreated node's
  startup gossip shadow round never receives the seed's reply and throws. This requires 2 pods
  with per-pod stable broadcast addresses and a delete+recreate of one node — multi-node
  orchestration that a single CQL `reproducer` string cannot express, hence this STUB.

Verbatim buggy signature (cass-1, cassandra:4.1.1, `kubectl logs --previous`):
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
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra16718(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.1.1"
    source_git_ref = "cassandra-4.1.1"
    # 4.1.1 already ships the bug, so deploy the stock image instead of an ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/locator/ReconnectableSnitchHelper.java"
    root_cause_description = (
        "Changing listen_address with prefer_local enabled may lead to issues. With a "
        "reconnectable snitch (prefer_local=true), Cassandra caches each peer's "
        "INTERNAL_ADDRESS_AND_PORT as preferred_ip in system.peers and routes outbound gossip "
        "there (OutboundConnectionSettings used SystemKeyspace.getPreferredIP). When a node keeps "
        "a stable broadcast/gossip identity but its internal (listen) address changes, the seed "
        "still routes to the OLD internal address, so the startup gossip shadow round reply never "
        "reaches the node and Gossiper.doShadowRound throws 'Unable to gossip with any peers'. "
        "The fix makes ReconnectableSnitchHelper.onDead() purge the stale INTERNAL_ADDRESS_AND_PORT "
        "and close the outbound connection, and resolves OutboundConnectionSettings via "
        "Gossiper.getInternalAddressAndPort instead of the cached preferred_ip."
    )

    # STUB: this bug needs a 2-pod ring with per-pod STABLE broadcast addresses and a
    # delete+recreate of one node so its listen_address changes while broadcast stays the
    # same. That is multi-node orchestration a single CQL string cannot express, so the
    # steps below are recorded verbatim from the reproduction evidence log rather than
    # flattened into CQL (a flattened version would compile and register but silently NOT
    # reproduce the bug). continuous_reproducer / crash_on_startup are intentionally left
    # at their defaults (False): the crash only fires after the multi-node trigger, so a
    # plain image-swap-and-wait would never observe it.
    reproducer = """
# CASSANDRA-16718 — multi-node reproduction (NOT a single-cluster CQL reproducer).
# Topology: 2 plain Cassandra pods (cassandra:4.1.1) pinned to one worker node, each
# fronted by its own ClusterIP Service so broadcast_address is a STABLE ClusterIP while
# listen_address rides the changing pod IP. prefer_local=true in
# cassandra-rackdc.properties; snitch = GossipingPropertyFileSnitch; no PVC (ephemeral).
#
# 1. Deploy 2 ClusterIP services (svc-0 = seed, svc-1) and 2 pods (cass-0, cass-1), each with:
#      cassandra-rackdc.properties = {dc=dc1, rack=rack1, prefer_local=true}
#      endpoint_snitch / snitch    = GossipingPropertyFileSnitch
#      CASSANDRA_BROADCAST_ADDRESS = its own service ClusterIP   (stable broadcast)
#      listen_address              = pod IP (auto)               (internal != broadcast)
#      seeds                       = svc-0 ClusterIP
#
# 2. Wait for the ring to form UN/UN (both nodes Up/Normal):
#      kubectl exec -n <ns> cass-0 -- nodetool status      # expect 2x UN
#
# 3. Confirm the precondition that arms the bug (internal != broadcast; preferred_ip non-null):
#      kubectl exec -n <ns> cass-1 -- cqlsh -e \
#        "SELECT broadcast_address, listen_address FROM system.local"
#        # broadcast_address = svc-1 ClusterIP, listen_address = cass-1 pod IP  (they differ)
#      kubectl exec -n <ns> cass-0 -- cqlsh -e \
#        "SELECT peer, preferred_ip FROM system.peers"
#        # peer = svc-1 ClusterIP, preferred_ip = cass-1 POD IP  (prefer_local cached the pod IP)
#
# 4. TRIGGER: delete cass-1 and recreate it. It comes back with a NEW pod IP (changed
#    listen_address) but the SAME broadcast ClusterIP (same gossip endpoint key):
#      kubectl delete pod -n <ns> cass-1
#      # recreate cass-1 from the same manifest (same svc-1 ClusterIP, new pod IP)
#
# 5. The seed retains the STALE preferred_ip = cass-1's OLD (now dead) pod IP:
#      kubectl exec -n <ns> cass-0 -- cqlsh -e \
#        "SELECT peer, preferred_ip FROM system.peers"
#        # preferred_ip still = the old pod IP, which no longer exists
#
# RESULT on cassandra:4.1.1 (buggy): cass-1 enters CrashLoopBackOff; the seed shows it DN.
# Its startup shadow round fails because the seed mis-routes the gossip reply to the stale
# internal address. `kubectl logs -n <ns> cass-1 --previous` shows:
#
#   Exception (java.lang.RuntimeException) encountered during startup: Unable to gossip with any peers
#   java.lang.RuntimeException: Unable to gossip with any peers
#       at org.apache.cassandra.gms.Gossiper.doShadowRound(Gossiper.java:1916)
#       at org.apache.cassandra.service.StorageService.checkForEndpointCollision(StorageService.java:694)
#       at org.apache.cassandra.service.StorageService.prepareToJoin(StorageService.java:996)
#       at org.apache.cassandra.service.StorageService.initServer(StorageService.java:842)
#       ...
#
# A/B control on cassandra:4.1.2 (fixed), identical steps: doShadowRound SUCCEEDS (the fix
# purges the stale internal address and resolves the current one), so startup proceeds PAST
# the shadow round and only stops at the unrelated endpoint-collision guard
# (StorageService.java:784 — "A node with address ... already exists"). The load-bearing
# contrast is doShadowRound THROWING on 4.1.1 vs RETURNING on 4.1.2.
"""
