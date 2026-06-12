"""CASSANDRA-16146: Node state incorrectly set to NORMAL after disablegossip+enablegossip during bootstrap.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16146
Buggy: 3.11.9  ->  Fixed: 3.11.10
Component: Cluster/Gossip

STUB: multi-node reproduction not yet encoded as a single-cluster Problem — see steps below.

Reproduction summary (2-node ring):
  A seed node reaches NORMAL while a second "joiner" node is parked in the BOOT/JOINING
  precondition (started with -Dcassandra.write_survey=true, which completes streaming then
  deliberately halts: "Startup complete, but write survey mode is active, not becoming an
  active ring member"; tokens are saved so getLocalTokens() is non-empty). An operator then
  runs `nodetool disablegossip` followed by `nodetool enablegossip` on the joiner. On 3.11.9
  there is no guard, so StorageService#setGossipTokens (reached via startGossiping()) blindly
  flips the gossip STATUS from BOOT to NORMAL — a node that never finished joining now reads
  as a Normal ring member eligible for reads/writes. The 3.11.10 fix adds an isNormal() guard
  in stopGossiping() that rejects disablegossip on a non-NORMAL node.

Verbatim buggy signature (from the reproduction evidence log):
  After the disablegossip/enablegossip dance the joiner advertises
    STATUS:87:NORMAL,-1077568207160367180        (was BOOT)
  while its own operation mode is still
    Mode: JOINING
  and the seed's `nodetool status` consequently flips the joiner from UJ to UN.

Why this is a STUB and not a flattened single-cluster reproducer:
  The irreducible part of this bug is a HETEROGENEOUS 2-pod precondition — a seed at NORMAL
  PLUS a joiner parked in BOOT/JOINING via JVM_EXTRA_OPTS=-Dcassandra.write_survey=true and
  CASSANDRA_SEEDS=<seed IP>. The GenericCustomBuildProblem deploy path provisions a single
  uniform operator-managed StatefulSet, so it cannot produce two pods with different JVM opts
  (write_survey must apply only to the joiner). The fault is also triggered via `nodetool`
  and observed via `nodetool gossipinfo`/`nodetool status`, not via a CQL query result.
  Flattening this into one CQL `reproducer` would compile and register but silently NOT
  reproduce the bug, which is worse than an honest stub. continuous_reproducer is therefore
  left False so no ReproducerPodMitigationOracle pod is created (the diagnosis LLM-as-a-judge
  oracle on the root cause remains active).
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra16146(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "3.11.9"
    source_git_ref = "cassandra-3.11.9"
    # 3.11.9 is fix-minus-one (fixed in 3.11.10), so the bug already ships in the stock
    # image — deploy the stock image instead of running a ~30-min `ant jar` source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/service/StorageService.java"
    root_cause_description = (
        "StorageService#setGossipTokens sets the gossip STATUS to NORMAL blindly. When an "
        "operator stops and re-starts gossip (nodetool disablegossip + enablegossip) on a node "
        "still in BOOT/JOINING — e.g. a node whose bootstrap halted or that is in write_survey "
        "mode — startGossiping() calls setGossipTokens(), which overrides the actual BOOT gossip "
        "state with NORMAL. The node then advertises STATUS=NORMAL while its operation mode is "
        "still JOINING, so the rest of the ring treats a never-joined node as a Normal member. "
        "The 3.11.10 fix adds an isNormal() guard in stopGossiping() that rejects disablegossip "
        "unless the node is in the NORMAL state."
    )

    # STUB reproducer: multi-node ring steps (NOT a single runnable CQL block). These are the
    # exact buggy-path steps from .claude/repro-evidence/repro-CASSANDRA-16146.md. They require
    # two distinct pods (a seed and a write_survey joiner) and `nodetool`, which a single
    # GenericCustomBuildProblem cluster cannot orchestrate — encoded here for documentation.
    reproducer = """
STUB: multi-node reproduction not yet encoded as a single-cluster Problem — see steps below.

# Topology: 2-node ring on cassandra:3.11.9, ephemeral storage.
#   cass-seed   : 3.11.9, reaches NORMAL.
#   cass-joiner : 3.11.9, started with JVM_EXTRA_OPTS=-Dcassandra.write_survey=true
#                 and CASSANDRA_SEEDS=<seed IP>. Two individual pods, NOT a uniform
#                 StatefulSet, so write_survey applies only to the joiner.

# 1. Bring up cass-seed and wait for it to reach NORMAL (UN in `nodetool status`).

# 2. Bring up cass-joiner pointing CASSANDRA_SEEDS at the seed IP and with
#    -Dcassandra.write_survey=true. It streams data, then parks before joining:
#      INFO StorageService.java - Bootstrap completed for tokens [...]
#      INFO StorageService.java - Startup complete, but write survey mode is active,
#                                 not becoming an active ring member. ...
#    Precondition now holds: operation mode JOINING, gossip STATUS=BOOT, tokens saved.

# 3. Confirm the BOOT/JOINING precondition on the joiner:
#      nodetool netstats | head -1        -> Mode: JOINING
#      nodetool gossipinfo (self entry)   -> STATUS:24:BOOT,<token>
#    And on the seed:
#      nodetool status                    -> UJ <joiner IP> ...   (Up/Joining)

# 4. THE DANCE on the joiner (both succeed on 3.11.9 — there is no guard):
nodetool disablegossip;
nodetool enablegossip;

# 5. THE BUG — observe on the joiner:
#      nodetool netstats | head -1        -> Mode: JOINING            (node STILL joining)
#      nodetool gossipinfo (self entry)   -> STATUS:87:NORMAL,-1077568207160367180
#                                            (gossip STATUS BLINDLY FLIPPED BOOT -> NORMAL)
#    And on the seed:
#      nodetool status                    -> UN <joiner IP> ...       (UJ -> UN: a node
#                                            that never finished joining now reads Normal)
"""
    # Multi-node / nodetool stub: no single-pod CQL reproducer pod to run, so do NOT enable
    # the continuous reproducer (it would deploy a pod that runs the prose above as CQL).
    continuous_reproducer = False
