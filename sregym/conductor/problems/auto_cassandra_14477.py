"""CASSANDRA-14477: spurious num_tokens-vs-initial_token check aborts startup.

Title: "The check of num_tokens against the length of initial_token in the yaml
triggers unexpectedly."

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-14477
Buggy: 3.11.8  ->  Fixed: observable on 3.11.19.
  (Jira lists fixVersion 3.11.9, but the released cassandra-3.11.9 source is
   BYTE-IDENTICAL to 3.11.8 for this code path — `Config.java:81` is still
   `public int num_tokens = 1;` and `applyInitialTokens()` still does the bare
   `tokens.size() != conf.num_tokens` check — so 3.11.9 is NOT actually fixed.
   The real fix, a nullable `Integer num_tokens` plus `applyTokensConfig()`, is
   present by 3.11.19, which was the A/B control used to reproduce this bug.)

Reproduction (single node):
  1. Remove `num_tokens` from cassandra.yaml so it is ABSENT (do NOT set it).
  2. Set `initial_token: 100,200` (two comma-separated tokens).
  3. Start cassandra (CASSANDRA_NUM_TOKENS env deliberately unset so the
     docker-entrypoint does NOT re-inject num_tokens).
  In 3.11.8 `applyInitialTokens()` compares the two-token list against the
  Config default `num_tokens = 1` and aborts startup with a misleading message
  that implies the operator set a conflicting num_tokens — even though the
  operator never set num_tokens at all. This is a startup crash, not a query
  error: the node never comes up.

Verbatim buggy signature (from the reproduction evidence log, §6):
  Exception (org.apache.cassandra.exceptions.ConfigurationException) encountered during startup: The number of initial tokens (by initial_token) specified is different from num_tokens value
"""

import logging
import shlex
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem

logger = logging.getLogger(__name__)


class AutoCassandra14477(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "3.11.8"
    source_git_ref = "cassandra-3.11.8"
    # 3.11.8 already ships the bug, so deploy the stock image instead of an
    # ~30-min `ant jar` build (the reproduction log confirmed the stock
    # cassandra:3.11.8 image reproduces this verbatim).
    prebuilt_from_stock = True

    # The check fires in DatabaseDescriptor.applyInitialTokens() (and the fix's
    # applyTokensConfig() landed here). The deeper enabler is Config.java:81,
    # `public int num_tokens = 1;` — a primitive int cannot distinguish
    # "unset in yaml" from "explicitly set to 1", so the comparison runs even
    # when the operator never configured num_tokens. The 3.11.19 fix makes
    # num_tokens a nullable Integer and only errors when tokens.size() != 1.
    root_cause_file = "src/java/org/apache/cassandra/config/DatabaseDescriptor.java"
    root_cause_description = (
        "Cassandra aborts startup with a misleading ConfigurationException — "
        "'The number of initial tokens (by initial_token) specified is different "
        "from num_tokens value' — when num_tokens is ABSENT from cassandra.yaml "
        "but initial_token lists more than one token (e.g. initial_token: 100,200). "
        "In DatabaseDescriptor.applyInitialTokens() (3.11.8, lines 945-956) the "
        "check `tokens.size() != conf.num_tokens` runs unconditionally, and "
        "conf.num_tokens has silently defaulted to the Config primitive-int default "
        "of 1 (Config.java:81, `public int num_tokens = 1;`), which cannot represent "
        "'unset'. So the check fires (2 != 1) and blames a num_tokens mismatch the "
        "operator never configured. CASSANDRA-10120 introduced this check; "
        "CASSANDRA-14477 reports it triggering unexpectedly. The fix (by 3.11.19) "
        "makes num_tokens a nullable Integer and moves the logic into "
        "applyTokensConfig(): when num_tokens is null and a single initial_token is "
        "given it infers num_tokens=1 and starts cleanly, otherwise it raises the "
        "accurate 'initial_token was set but num_tokens is not!' message."
    )

    # Startup-crash bug: the node fails during config parse, never reaching CQL.
    # inject_fault() (in GenericCustomBuildProblem) therefore: runs
    # setup_preconditions() while the stock binary is still up, swaps in the
    # buggy 3.11.8 image, and waits for CrashLoopBackOff instead of Ready.
    crash_on_startup = True

    # continuous_reproducer MUST stay False here. The crash_on_startup branch of
    # GenericCustomBuildProblem.inject_fault() returns immediately after
    # inject_buggy_image_expect_crash() and never deploys a continuous reproducer
    # pod. Setting this True would still attach a ReproducerPodMitigationOracle
    # pointed at a pod that is never created — an unsatisfiable mitigation oracle.
    # False => diagnosis-only, the correct grading shape for a startup-crash bug.
    continuous_reproducer = False

    # The canonical reproduction is a cassandra.yaml mutation, not CQL: the node
    # crashes before any client can connect. For crash_on_startup bugs this
    # string is DOCUMENTARY ONLY — inject_fault() does not execute it — but it is
    # kept as the verbatim reproducer from the evidence log (the per-pod command
    # that mutates the effective token config before docker-entrypoint runs).
    reproducer = """
# Remove num_tokens so it is ABSENT, then set two initial_tokens, then start
# cassandra. CASSANDRA_NUM_TOKENS is deliberately left unset so the
# docker-entrypoint does not re-inject num_tokens.
sed -i '/^num_tokens:/d' /etc/cassandra/cassandra.yaml;
echo 'initial_token: 100,200' >> /etc/cassandra/cassandra.yaml;
grep -nE '^(num_tokens|initial_token)' /etc/cassandra/cassandra.yaml || echo 'num_tokens ABSENT';
exec docker-entrypoint.sh cassandra -f;
"""

    def setup_preconditions(self):
        """Apply the token-config mutation that triggers the spurious startup abort.

        Mirrors the per-pod command used in the reproduction evidence log: delete
        any `num_tokens:` line from each Cassandra pod's effective cassandra.yaml
        and append `initial_token: 100,200`, so that on the next (buggy-image)
        restart applyInitialTokens() compares tokens.size()=2 against the Config
        default num_tokens=1 and aborts.

        NOTE (inherent harness limitation, documented per the skill): the standard
        K8ssandra operator deploy path used by GenericCustomBuildProblem does not
        expose a first-class hook for arbitrary cassandra.yaml lines or a custom
        pod command, so this best-effort mutation edits the file in the live
        cassandra container(s) via kubectl exec. It cannot be statically verified
        in code-gen mode and is not guaranteed to survive the operator's config
        reconciliation on every K8ssandra version; the authoritative, reproduced
        form of this config is the per-pod command captured in `reproducer` above.
        """
        try:
            out = subprocess.run(
                f"kubectl get pods -n {self.namespace} "
                f"-l app.kubernetes.io/instance={self.app.cluster_name} "
                f"--no-headers -o custom-columns=NAME:.metadata.name",
                shell=True, capture_output=True, text=True,
            )
            pods = [p.strip() for p in out.stdout.splitlines() if p.strip()]
        except Exception as e:
            logger.warning(f"[AutoCassandra14477] Could not list Cassandra pods: {e}")
            pods = []

        if not pods:
            logger.warning(
                "[AutoCassandra14477] No Cassandra pods found to apply token config — "
                "skipping setup_preconditions (see `reproducer` for the canonical command)"
            )
            return

        # Common cassandra.yaml locations across the cass-management-api images.
        yaml_paths = "/etc/cassandra/cassandra.yaml /opt/cassandra/conf/cassandra.yaml"
        mutate = (
            "for f in " + yaml_paths + "; do "
            "[ -f \"$f\" ] || continue; "
            "sed -i '/^num_tokens:/d' \"$f\"; "
            "grep -q '^initial_token:' \"$f\" "
            "&& sed -i 's/^initial_token:.*/initial_token: 100,200/' \"$f\" "
            "|| echo 'initial_token: 100,200' >> \"$f\"; "
            "done"
        )
        for pod in pods:
            logger.info(
                f"[AutoCassandra14477] Applying token config (num_tokens absent, "
                f"initial_token: 100,200) to pod {pod}"
            )
            subprocess.run(
                f"kubectl exec -n {self.namespace} {pod} -c cassandra -- "
                f"bash -c {shlex.quote(mutate)}",
                shell=True, capture_output=True, text=True,
            )
