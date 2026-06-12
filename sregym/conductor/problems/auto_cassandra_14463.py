"""STUB: config-gated single-node startup-guard bug — NOT honestly encodable as a runnable
single-cluster Problem with the current cass-operator machinery; see the WHY section below.

CASSANDRA-14463: Prevent the generation of new tokens when using the replace_address flag.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-14463

Buggy: 4.0.0.  Fixed: 3.0.25, 3.11.11, 4.0.1, 4.1-alpha1, 4.1 (A/B control = cassandra:4.0.1).

Reproduction summary (config-gated, SINGLE node, startup guard — confirmed single-node in the
evidence log section 5; the "ring" tag hint is wrong):
  Start a node with `cassandra.replace_address` set (JVM_EXTRA_OPTS=-Dcassandra.replace_address=
  <ip>) while the node is in its OWN seed list (the stock docker-entrypoint makes a lone pod its
  own seed: `: ${CASSANDRA_SEEDS:="$CASSANDRA_BROADCAST_ADDRESS"}`), with auto_bootstrap=true and
  initial_token unset. On 4.0.0 the seed+replace combination is NOT blocked at startup — the node
  proceeds into prepareForReplacement() replacement logic and (with a REAL dead-node IP) would skip
  streaming and generate a fresh random token set instead of inheriting the dead node's tokens. On
  4.0.1 the node REFUSES to start in this configuration unless -Dcassandra.allow_unsafe_replace=true.
  The discriminating code change is StorageService.prepareForReplacement():
    - 4.0.0 guard: `if (!isAutoBootstrap() && !allow_unsafe_replace) throw ...`  — not taken when
      auto_bootstrap=true (the default), so a seed proceeds.
    - 4.0.1 guard: `if (!shouldBootstrap() && !allow_unsafe_replace) throw ...` where
      `shouldBootstrap() = isAutoBootstrap() && !bootstrapComplete() && !isSeed()`, so a seed
      (isSeed()==true => shouldBootstrap()==false) is blocked.

Verbatim buggy signature — cassandra:4.0.0 (PASSED the seed+replace guard, then died downstream
only because the dummy target isn't in gossip; the consistency-guarantee guard string appears
0 times in the 4.0.0 log):
  INFO  [main] StorageService.java:528 - Gathering node replacement information for /10.255.255.254:7000
  Exception (java.lang.RuntimeException) encountered during startup: Cannot replace_address
      /10.255.255.254:7000 because it doesn't exist in gossip
  java.lang.RuntimeException: Cannot replace_address /10.255.255.254:7000 because it doesn't exist in gossip
    at org.apache.cassandra.service.StorageService.prepareForReplacement(StorageService.java:533)
    at org.apache.cassandra.service.StorageService.prepareToJoin(StorageService.java:911)
    at org.apache.cassandra.service.StorageService.initServer(StorageService.java:784)
    at org.apache.cassandra.service.StorageService.initServer(StorageService.java:729)
    at org.apache.cassandra.service.CassandraDaemon.setup(CassandraDaemon.java:420)
    at org.apache.cassandra.service.CassandraDaemon.activate(CassandraDaemon.java:763)
    at org.apache.cassandra.service.CassandraDaemon.main(CassandraDaemon.java:887)
The single telling line is `StorageService.java:528 - Gathering node replacement information` —
4.0.0 reaches replacement logic; on 4.0.1 that line appears 0 times because it refuses at the guard
(StorageService.java:522) with "Replacing a node without bootstrapping ... restart with
-Dcassandra.allow_unsafe_replace=true".

WHY THIS IS A STUB (do not flatten into a single-cluster crash_on_startup Problem — it would
register but silently NOT discriminate the bug, which is worse than an honest stub):

  1. DISPOSITIVE — with the dummy replace IP that the reproduction actually uses (10.255.255.254,
     not in gossip), BOTH builds CRASH on startup. 4.0.0 crashes *downstream* of the guard
     ("Cannot replace_address ... doesn't exist in gossip"); 4.0.1 crashes *at* the guard
     ("Replacing a node without bootstrapping ..."). The ONLY discriminator is the log message
     (line 528 / "Gathering node replacement information" present vs. absent). SREGym's standard
     oracles cannot read it: inject_buggy_image_expect_crash() only waits for CrashLoopBackOff
     (both builds produce it), and ReproducerPodMitigationOracle reads pod readiness (both NotReady).
     Discriminating WITHOUT a log grep requires a REAL dead-node IP so the buggy node survives the
     guard and joins with fresh tokens while the fixed node refuses — and a real dead node means
     bringing up a ring, killing a member, then replacing it: multi-node orchestration a single
     reproducer string cannot express.

  2. The trigger is a JVM SYSTEM PROPERTY (-Dcassandra.replace_address=...), not a cassandra.yaml
     key. Unlike the config-gated startup precedents (auto_cassandra_17933 zero-byte audit file on
     the PVC, auto_cassandra_15135 poisoned system_schema.indexes), there is no cassandraYaml
     passthrough for arbitrary -D flags, and GenericCustomBuildProblem's inject_buggy_image_expect_
     crash() swaps only the image — it sets no env/JVM_EXTRA_OPTS.

  3. The guard fires ONLY when the node is its OWN seed (isSeed()==true). The reproduction relied on
     the stock docker-entrypoint default (a lone pod seeds itself). cass-operator runs the
     management-api entrypoint and computes seeds via its seed service, so an operator-managed node
     is not its own seed and the seed+replace condition cannot be constructed through the CR.

Because of (1)–(3) this is registered as a DIAGNOSIS-ONLY stub: it carries the buggy version,
source ref, root-cause file/description, and the full reproduction steps verbatim (in `reproducer`)
so a future log-aware or multi-node harness can promote it to a runnable Problem. crash_on_startup
and continuous_reproducer are deliberately False (a stock 4.0.0 node deployed via cass-operator,
without replace_address and with operator-managed seeds, starts NORMALLY — so neither a startup
crash nor a looping reproducer pod would materialize, and any oracle keyed on them would falsely
grade). The LLMAsAJudgeOracle on the root cause is the only grader.
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra14463(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.0.0"
    source_git_ref = "cassandra-4.0.0"
    # Buggy version = fix patch (4.0.1) - 1; the bug already ships in the stock 4.0.0 image,
    # so re-tag the stock image instead of an ~30-min ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/service/StorageService.java"
    root_cause_description = (
        "In StorageService.prepareForReplacement(), Cassandra 4.0.0 guards the unsafe "
        "non-bootstrapping replacement path with `if (!DatabaseDescriptor.isAutoBootstrap() && "
        "!cassandra.allow_unsafe_replace) throw ...`. Because auto_bootstrap defaults to true, the "
        "guard is not taken even when the replacing node is itself a seed (isSeed()==true), so the "
        "node enters replacement logic; with a real dead-node IP it then skips streaming (a seed "
        "does not bootstrap) and generates a fresh random token set instead of inheriting the dead "
        "node's tokens, silently joining the ring with wrong tokens. The fix (4.0.1) changes the "
        "guard to `if (!shouldBootstrap() && !cassandra.allow_unsafe_replace) throw ...`, where "
        "shouldBootstrap() = isAutoBootstrap() && !bootstrapComplete() && !isSeed(), so a seed is "
        "blocked from replacing without -Dcassandra.allow_unsafe_replace=true."
    )

    # STUB: prose + A/B steps, NOT a runnable single-cluster reproducer. See the module docstring
    # for WHY this config-gated single-node startup-guard bug cannot be honestly run by SREGym's
    # current machinery (dummy-IP path crashes on BOTH builds; the only discriminator is a log
    # line; the trigger is a JVM -D flag plus seed membership, neither expressible via the
    # operator-managed CR). Transcribed verbatim so a future log-aware / multi-node harness can
    # promote it to a runnable Problem.
    reproducer = """
-- STUB: CASSANDRA-14463 — config-gated SINGLE-NODE startup-guard bug. NOT runnable via the
-- cqlsh reproducer runner and NOT a CQL block: the bug fires during node startup, gated by a
-- JVM system property and seed membership, before CQL is reachable.
--
-- Preconditions (all satisfied by the stock single-pod docker-entrypoint; NOT by cass-operator):
--   * Node is in its OWN seed list   (entrypoint default: CASSANDRA_SEEDS=$CASSANDRA_BROADCAST_ADDRESS
--                                      => isSeed()==true)
--   * auto_bootstrap = true          (default)
--   * initial_token unset            (default; num_tokens=16)
--   * cassandra.replace_address set  (JVM_EXTRA_OPTS=-Dcassandra.replace_address=10.255.255.254)
--
-- A/B run (identical env; only the image tag differs):
--   kubectl create namespace repro-14463
--   # buggy pod:   image cassandra:4.0.0 ; env JVM_EXTRA_OPTS=-Dcassandra.replace_address=10.255.255.254 ; CASSANDRA_SEEDS unset
--   # control pod: image cassandra:4.0.1 ; identical env
--   kubectl logs -n repro-14463 cass-buggy     # 4.0.0
--   kubectl logs -n repro-14463 cass-control   # 4.0.1
--
-- Expected (buggy 4.0.0): passes the seed+replace guard and enters replacement logic —
--   INFO  StorageService.java:528 - Gathering node replacement information for /10.255.255.254:7000
--   (then, ONLY because the dummy target isn't in gossip, dies downstream at
--    StorageService.java:533: "Cannot replace_address /10.255.255.254:7000 because it doesn't
--    exist in gossip"). With a REAL dead-node IP this un-blocked path joins the ring with a fresh
--    random token set instead of the dead node's tokens — the actual CASSANDRA-14463 fault.
-- Expected (fixed 4.0.1): refuses to start AT the guard (StorageService.java:522):
--   "Replacing a node without bootstrapping risks invalidating consistency guarantees ... To
--    perform this operation, please restart with -Dcassandra.allow_unsafe_replace=true"
--   and "Gathering node replacement information" appears 0 times.
"""

    # Diagnosis-only stub: NOT a startup-crash Problem and NOT a continuous reproducer (see the
    # module docstring). A stock 4.0.0 node deployed via cass-operator — without replace_address
    # and with operator-managed seeds — starts NORMALLY, so neither inject_buggy_image_expect_crash
    # (would time out waiting for a crash) nor a looping reproducer pod is appropriate. With both
    # False, GenericCustomBuildProblem sets mitigation_oracle = None and grades only via the
    # LLMAsAJudgeOracle on the root cause.
    crash_on_startup = False
    continuous_reproducer = False
