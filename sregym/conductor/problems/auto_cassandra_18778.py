"""Empty keystore_password no longer allowed on encryption_options.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-18778

Title: Empty keystore_password no longer allowed on encryption_options.

Buggy: 4.1.3 (post CASSANDRA-18124, which landed in 4.1.2 & 5.0).
Fixed: 4.1.4 (also 5.0-alpha1 / 5.0).

Reproduction summary (config-gated, single node, startup failure):
  1. Create a PKCS12 keystore with an EMPTY store password. keytool cannot *create*
     such a keystore (it requires a >=6-char key password), but PKCS12 keystores
     produced by other tools (e.g. openssl `-passout pass:`) can have empty passwords
     and were historically readable.
  2. In cassandra.yaml client_encryption_options set enabled: true, point keystore at
     that file, and set keystore_password: "" (empty string).
  3. Start Cassandra. After CASSANDRA-18124 the FileBasedSslContextFactory.validatePassword
     check rejects an EMPTY (not just null) keystore_password, so SSL context validation
     fails during daemon initialization and the node aborts before listening for clients.
  The fix (4.1.4) makes validatePassword reject only a null password again, so an empty
  keystore_password with a valid empty-password keystore is accepted (CQL comes up encrypted).

Verbatim buggy signature (from the reproduction evidence log; copied from
`kubectl logs` of our own crashing 4.1.3 pod):
  Exception (org.apache.cassandra.exceptions.ConfigurationException) encountered during startup: Failed to initialize SSL
  org.apache.cassandra.exceptions.ConfigurationException: Failed to initialize SSL
    at org.apache.cassandra.security.SSLFactory.validateSslContext(SSLFactory.java:405)
  Caused by: java.lang.IllegalArgumentException: 'keystore_password' must be specified
    at org.apache.cassandra.security.FileBasedSslContextFactory.validatePassword(FileBasedSslContextFactory.java:133)
    at org.apache.cassandra.security.FileBasedSslContextFactory.buildKeyManagerFactory(FileBasedSslContextFactory.java:151)
    at org.apache.cassandra.security.SSLFactory.createNettySslContext(SSLFactory.java:168)
    at org.apache.cassandra.security.SSLFactory.validateSslContext(SSLFactory.java:355)
  ERROR [main] CassandraDaemon.java:897 - Exception encountered during startup
  (the node never reaches CQL; the pod goes Pending -> Failed/Error and never becomes Ready)

Runtime-fidelity note: the evidence log reproduced this on a single bare pod that mounted a
host-generated empty-password keystore as a Secret and set the cassandra.yaml encryption
block via a pod `command` override. The SREGym runtime instead deploys a 3-node
K8ssandra-operator cluster, so the empty-password keystore is generated on the host and
planted on the data PVC (post_deploy, via kubectl exec + base64 — keytool inside the
cass-management-api image has NO openssl and refuses to create empty-password keystores),
and client_encryption_options is enabled via the operator-owned CR cassandraYaml block
(setup_preconditions) so both survive the buggy-image rolling restart. The bug itself is
single-node by nature (a per-node SSL-context validation at startup); the 3-node cluster is
incidental infrastructure.

Sequencing note (why the keystore is planted in post_deploy but encryption is enabled in
setup_preconditions): the deployed/stock image IS the buggy 4.1.3 (the bug already ships in
the released image, fix is 4.1.4, so prebuilt_from_stock re-tags 4.1.3). There is therefore
no healthy binary to deploy first — enabling encryption with keystore_password: "" crashes
4.1.3 regardless of which tag the container runs. So post_deploy() only PLANTS the keystore
(harmless; the node stays Ready with encryption still off), and setup_preconditions() ENABLES
client_encryption_options right before the buggy-image swap, arming the startup crash. This
mirrors auto_cassandra_21290, which likewise arms the fatal state in setup_preconditions
(not post_deploy) to keep the stock node healthy until the controlled crash step.
"""

import base64
import logging
import subprocess
import tempfile

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem

logger = logging.getLogger(__name__)

# PVC-backed keystore path. The K8ssandra data volume is mounted at /var/lib/cassandra,
# which survives the operator's rolling restart, so a keystore planted here is the file the
# restarted (buggy) node reads when it validates the SSL context.
_KEYSTORE_FILE = "/var/lib/cassandra/keystore.p12"
_KEYSTORE_DIR = "/var/lib/cassandra"


class AutoCassandra18778(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.1.3"
    source_git_ref = "cassandra-4.1.3"
    # 4.1.3 already ships the bug (the empty-password rejection landed in 4.1.2 via
    # CASSANDRA-18124; the fix is 4.1.4), so deploy/re-tag the stock image instead of an
    # ~30-min ant-jar build.
    prebuilt_from_stock = True

    # Deepest Cassandra frame in the verbatim signature: validatePassword rejects an EMPTY
    # keystore_password, which buildKeyManagerFactory -> SSLFactory.createNettySslContext ->
    # validateSslContext surfaces as ConfigurationException "Failed to initialize SSL". The
    # 4.1.4 fix relaxes this check to reject only a null (not empty) password.
    root_cause_file = "src/java/org/apache/cassandra/security/FileBasedSslContextFactory.java"
    root_cause_description = (
        "After CASSANDRA-18124 (shipped in 4.1.2 and 5.0), "
        "FileBasedSslContextFactory.validatePassword rejects an EMPTY keystore_password "
        "(not just a null one). With client_encryption_options enabled and a valid "
        "empty-password PKCS12 keystore plus keystore_password: \"\", "
        "buildKeyManagerFactory calls validatePassword, which throws "
        "IllegalArgumentException: 'keystore_password' must be specified. "
        "SSLFactory.createNettySslContext / validateSslContext wrap this as "
        "ConfigurationException 'Failed to initialize SSL', aborting daemon startup before "
        "the node listens for CQL clients (the pod never becomes Ready). The fix (4.1.4 / "
        "5.0) makes validatePassword reject only a null password again, so an empty "
        "keystore_password backed by a valid empty-password keystore is accepted."
    )

    # This is a config-gated STARTUP-CRASH bug, not a query-time bug. The crash_on_startup
    # branch in GenericCustomBuildProblem.inject_fault() runs setup_preconditions() (below)
    # while the (stock 4.1.3) binary is still up, then swaps in the buggy image and waits for
    # the node to enter CrashLoopBackOff -- it never executes `reproducer` as CQL. The string
    # below therefore documents the manual reproduction steps rather than runnable CQL.
    reproducer = """
-- CASSANDRA-18778 reproduction (config-gated startup failure; NOT executable CQL).
-- An empty-password PKCS12 keystore is generated on the host and planted on the data PVC
-- (post_deploy), client_encryption_options is enabled with keystore_password: "" via the
-- K8ssandraCluster CR (setup_preconditions), then the buggy-image swap restarts the node,
-- which fails SSL-context validation during startup and never reaches CQL.
--
-- 1. Create a PKCS12 keystore with an EMPTY store password (keytool cannot -- use openssl):
--      openssl req -x509 -newkey rsa:2048 -nodes -keyout k.pem -out c.pem -subj /CN=cass -days 365
--      openssl pkcs12 -export -in c.pem -inkey k.pem -out keystore.p12 -passout pass:
--    (verify it reads with an empty store password:
--      keytool -list -keystore keystore.p12 -storetype PKCS12 -storepass "")
-- 2. Enable client_encryption_options in cassandra.yaml pointing at that keystore, with an
--    empty keystore_password:
--      client_encryption_options:
--        enabled: true
--        optional: false
--        keystore: /var/lib/cassandra/keystore.p12
--        keystore_password: ""
-- 3. (Re)start the node. Startup aborts with
--      ConfigurationException: Failed to initialize SSL
--      Caused by: IllegalArgumentException: 'keystore_password' must be specified
--        at FileBasedSslContextFactory.validatePassword(FileBasedSslContextFactory.java:133)
--    and the node never listens for CQL clients (pod Pending -> Failed/Error, never Ready).
--
-- A/B control (per the evidence log): the SAME empty keystore_password + same empty-password
-- keystore on the fixed image (4.1.4) starts cleanly -- CQL comes up "(encrypted)", "Startup
-- complete", 0 occurrences of "must be specified" -- isolating the empty-password rejection
-- as the sole trigger.
"""

    # Startup-crash bug: inject runs preconditions on the running binary, swaps to the buggy
    # image, and waits for CrashLoopBackOff rather than a Ready pod.
    crash_on_startup = True
    # Diagnosis-only. The crash_on_startup branch of inject_fault() returns before
    # deploy_continuous_reproducer(), so the {cluster_name}-reproducer Deployment that
    # ReproducerPodMitigationOracle inspects is never created -- a mitigation oracle would
    # hit its 404 branch (success=False) for BOTH the buggy and the fixed build and could
    # not discriminate. Leaving continuous_reproducer False makes mitigation_oracle = None
    # (diagnosis graded by LLMAsAJudgeOracle on the root cause), matching the
    # auto_cassandra_17933 / auto_cassandra_21290 / etcd / tidb crash_on_startup precedents.
    # No expected_output (this is a crash, not a wrong result).
    continuous_reproducer = False

    def post_deploy(self):
        """Plant a valid empty-password PKCS12 keystore on every Cassandra pod's PVC-backed
        data directory while the node is still healthy (encryption still OFF).

        The keystore must be generated OFF the pod: the cass-management-api image has no
        openssl and keytool refuses to create a keystore with an empty/short key password
        ("Key password must be at least 6 characters"). We therefore generate it on the host
        with openssl, base64-encode it, and decode it into place inside each pod. The file
        lives under /var/lib/cassandra (the data PVC), so it survives the later buggy-image
        rolling restart and is the file the restarted node reads.

        Encryption is intentionally NOT enabled here: the deployed/stock image is the buggy
        4.1.3, so enabling client_encryption_options with keystore_password: "" would crash
        the node during this very deploy phase. Enabling it is deferred to
        setup_preconditions(), which runs immediately before the controlled buggy-image swap.
        """
        pods = self._cassandra_pods()
        if not pods:
            logger.warning(
                "[AutoCassandra18778] No Cassandra pods found in namespace "
                f"{self.namespace!r} -- cannot plant empty-password keystore"
            )
            return

        keystore_b64 = self._generate_empty_password_pkcs12_b64()
        if not keystore_b64:
            logger.warning(
                "[AutoCassandra18778] Could not generate empty-password keystore "
                "(host openssl unavailable?) -- the startup crash will not reproduce"
            )
            return

        # Decode the host-generated keystore into the PVC path on every pod and verify it
        # reads with an EMPTY store password (mirrors the evidence-log verification).
        plant_script = (
            f"mkdir -p {_KEYSTORE_DIR}; "
            f"printf '%s' '{keystore_b64}' | base64 -d > {_KEYSTORE_FILE}; "
            f"ls -l {_KEYSTORE_FILE}; "
            f"keytool -list -keystore {_KEYSTORE_FILE} -storetype PKCS12 -storepass '' "
            f"2>&1 | head -4"
        )

        for pod in pods:
            logger.info(
                f"[AutoCassandra18778] Planting empty-password {_KEYSTORE_FILE} on pod {pod}"
            )
            cmd = (
                f"kubectl exec -n {self.namespace} {pod} -c cassandra -- "
                f"bash -c {subprocess.list2cmdline([plant_script])}"
            )
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode != 0:
                logger.warning(
                    f"[AutoCassandra18778] Planting keystore on {pod} returned "
                    f"{result.returncode}: {result.stderr.strip()}"
                )
            else:
                logger.info(
                    f"[AutoCassandra18778] Keystore on {pod}:\n{result.stdout.strip()}"
                )

    def setup_preconditions(self):
        """Enable client_encryption_options (with empty keystore_password) on the deployed
        cluster via the K8ssandraCluster CR, arming the startup crash.

        client_encryption_options is a cassandra.yaml STARTUP setting. It must be enabled
        through the operator-owned CR cassandraYaml block -- NOT by editing cassandra.yaml
        inside the running pod, because the operator's config-builder re-renders cassandra.yaml
        from the CR on every reconcile / image swap, so a kubectl-exec edit to the live file
        would be wiped on the very restart that triggers the crash. Patching the CR makes the
        encryption config persist across the buggy-image rolling restart, which is what runs
        the SSL-context validation at startup and trips on the empty keystore_password.

        keystore points at the PVC-backed path planted by post_deploy(), and keystore_password
        is the empty string -- the exact state the buggy 4.1.3 build rejects. This is run in
        setup_preconditions (just before inject_buggy_image_expect_crash) rather than in
        post_deploy because the stock image is itself the buggy 4.1.3, so enabling encryption
        any earlier would crash the node during the deploy phase instead of at the controlled
        crash step.
        """
        cluster = self.app.cluster_name
        ns = self.namespace
        logger.info(
            f"[AutoCassandra18778] Enabling client_encryption_options on K8ssandraCluster "
            f"'{cluster}' in {ns} (cassandraYaml.client_encryption_options.enabled=true, "
            f"keystore={_KEYSTORE_FILE}, keystore_password=\"\")"
        )
        # Cluster-level cassandraYaml passthrough (applies to all datacenters).
        # client_encryption_options is a structured cassandra.yaml key, so it is supplied as a
        # nested object. keystore_password is the empty string -- the trigger for this bug.
        patch = (
            '{"spec":{"cassandra":{"config":{"cassandraYaml":'
            '{"client_encryption_options":{"enabled":true,"optional":false,'
            f'"keystore":"{_KEYSTORE_FILE}","keystore_password":""'
            "}}}}}}"
        )
        result = subprocess.run(
            f"kubectl patch k8ssandracluster {cluster} -n {ns} "
            f"--type=merge -p '{patch}'",
            shell=True, capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.warning(
                f"[AutoCassandra18778] enable client_encryption_options patch failed: "
                f"{result.stderr.strip()[:300]}"
            )
            return
        logger.info(
            "[AutoCassandra18778] client_encryption_options enabled with empty "
            "keystore_password; the subsequent buggy-image swap will trip SSL-context "
            "validation at startup"
        )

    @staticmethod
    def _generate_empty_password_pkcs12_b64() -> str | None:
        """Generate a valid empty-password PKCS12 keystore on the host with openssl and return
        its base64 encoding (or None if openssl is unavailable).

        keytool cannot create an empty-password keystore (it requires a >=6-char key
        password), and the cass-management-api image has no openssl, so the keystore is
        produced on the host and transferred into the pod base64-encoded. The empty-password
        PKCS12 is exactly the keystore shape the evidence log used to trigger the bug.
        """
        with tempfile.TemporaryDirectory() as tmp:
            key = f"{tmp}/k.pem"
            cert = f"{tmp}/c.pem"
            ks = f"{tmp}/keystore.p12"
            gen = (
                f"openssl req -x509 -newkey rsa:2048 -nodes -keyout {key} -out {cert} "
                f"-subj /CN=cass -days 365 && "
                f"openssl pkcs12 -export -in {cert} -inkey {key} -out {ks} -passout pass:"
            )
            result = subprocess.run(gen, shell=True, capture_output=True, text=True)
            if result.returncode != 0:
                logger.warning(
                    f"[AutoCassandra18778] openssl keystore generation failed: "
                    f"{result.stderr.strip()[:300]}"
                )
                return None
            try:
                with open(ks, "rb") as f:
                    return base64.b64encode(f.read()).decode("ascii")
            except OSError as e:
                logger.warning(f"[AutoCassandra18778] reading generated keystore failed: {e}")
                return None

    def _cassandra_pods(self) -> list[str]:
        """Return the Cassandra StatefulSet pod names for this cluster."""
        out = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-l app.kubernetes.io/managed-by=cass-operator "
            f"-o jsonpath='{{.items[*].metadata.name}}'",
            shell=True, capture_output=True, text=True,
        ).stdout.strip().strip("'")
        pods = [p for p in out.split() if p]
        if pods:
            return pods
        # Fallback selector if the managed-by label differs across operator versions.
        out = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-l app.kubernetes.io/name=cassandra "
            f"-o jsonpath='{{.items[*].metadata.name}}'",
            shell=True, capture_output=True, text=True,
        ).stdout.strip().strip("'")
        return [p for p in out.split() if p]
