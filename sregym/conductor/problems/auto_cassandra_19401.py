"""CASSANDRA-19401: nodetool import silently imports nothing from a flat source directory.

Title: Nodetool import expects directory structure
JIRA:  https://issues.apache.org/jira/browse/CASSANDRA-19401
Buggy: 4.1.4  ->  Fixed: 4.0.13, 4.1.5, 5.0-rc1, 6.0
Components: Local/SSTable

Reproduction summary (single node, NODETOOL/FILESYSTEM SEQUENCE — not pure CQL):
  The 4.1 docs claim `nodetool import` does NOT require SSTables to live in a
  `<keyspace>/<table>` directory because the keyspace/table are given on the command
  line. In reality, on 4.1.4, when the source directory is a FLAT directory whose parent
  dir names do NOT match `<keyspace>/<table>`, `nodetool import --copy-data` silently
  imports nothing (nodetool exits 0 with no stdout) and the table stays empty. Moving the
  exact same SSTables into a `.../<keyspace>/<table>/`-named directory makes the import
  succeed, and on 4.1.5 the identical flat-path import succeeds — isolating the failure to
  import-path handling in SSTableImporter.

Verbatim buggy signature (server-side INFO log on cassandra:4.1.4):
  SSTableImporter.java:173 - No new SSTables were found for repro19401ks/t

This is encoded with a custom inject_fault() (kubectl-exec into the Cassandra server pod)
because the reproduction needs nodetool flush/import plus on-disk SSTable staging that a
pure-CQL `reproducer` string cannot express. The staging dir must be chown'd to the
Cassandra daemon uid (999) — kubectl exec runs as root, so without the chown the importer
fails earlier with `Insufficient permissions on directory` (a separate guard at
SSTableImporter.java:242, NOT this bug). It is diagnosis-only (continuous_reproducer=False):
the standard continuous-reproducer pod is a separate CQL client that cannot run nodetool or
see the server's data dir, so a CQL-loop mitigation probe could not observe this bug.
"""

import base64
import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_KEYSPACE = "repro19401ks"
_TABLE = "t"
_STAGING_DIR = "/tmp/staging"
_DATA_DIR = "/var/lib/cassandra/data"
_CASS_UID = "999"  # the cassandra daemon runs as uid 999 inside the pod

# CQL run before the flush/import sequence: create the keyspace + table and insert 5 rows.
_SETUP_CQL = (
    "DROP KEYSPACE IF EXISTS repro19401ks; "
    "CREATE KEYSPACE repro19401ks WITH REPLICATION = "
    "{'class': 'SimpleStrategy', 'replication_factor': 1}; "
    "CREATE TABLE repro19401ks.t (id int PRIMARY KEY, v text); "
    "INSERT INTO repro19401ks.t (id, v) VALUES (1, 'a'); "
    "INSERT INTO repro19401ks.t (id, v) VALUES (2, 'b'); "
    "INSERT INTO repro19401ks.t (id, v) VALUES (3, 'c'); "
    "INSERT INTO repro19401ks.t (id, v) VALUES (4, 'd'); "
    "INSERT INTO repro19401ks.t (id, v) VALUES (5, 'e');"
)

# CQL run after staging: empty the table so the (failed) import result is client-visible.
_TRUNCATE_CQL = "TRUNCATE repro19401ks.t;"

# CQL to observe the buggy result (table stays empty after the flat-path import).
_COUNT_CQL = "SELECT count(*) FROM repro19401ks.t;"


class AutoCassandra19401(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.1.4"
    source_git_ref = "cassandra-4.1.4"
    # 4.1.4 already ships the bug, so deploy the stock image instead of an ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/db/SSTableImporter.java"
    root_cause_description = (
        "nodetool import (StorageService.importNewSSTables -> SSTableImporter) does not honor the "
        "documented contract that SSTables may live in a flat source directory when keyspace/table "
        "are passed on the command line. On 4.1.4, SSTableImporter only discovers SSTables whose "
        "parent directory is named <keyspace>/<table>; given a flat source dir it finds nothing and "
        "logs 'No new SSTables were found for repro19401ks/t' (SSTableImporter.java:173), so "
        "`nodetool import --copy-data` exits 0 having imported nothing and the table stays empty. "
        "The same SSTables import correctly from a <keyspace>/<table>-named directory on 4.1.4, and "
        "the identical flat-path import succeeds on 4.1.5, pinning the defect to SSTableImporter's "
        "directory-name-dependent SSTable discovery. The fix makes import discover SSTables in the "
        "given source directory regardless of its directory naming."
    )

    # Documented buggy reproducer (run programmatically by inject_fault inside the server pod,
    # because it mixes CQL with nodetool flush/import and on-disk SSTable staging). The discriminator
    # is the FLAT source dir /tmp/staging whose parent dirs are NOT <keyspace>/<table>.
    reproducer = """
-- 1. Schema + data (5 rows), then flush to write SSTables to disk:
DROP KEYSPACE IF EXISTS repro19401ks;
CREATE KEYSPACE repro19401ks WITH REPLICATION = {'class': 'SimpleStrategy', 'replication_factor': 1};
CREATE TABLE repro19401ks.t (id int PRIMARY KEY, v text);
INSERT INTO repro19401ks.t (id, v) VALUES (1, 'a');
INSERT INTO repro19401ks.t (id, v) VALUES (2, 'b');
INSERT INTO repro19401ks.t (id, v) VALUES (3, 'c');
INSERT INTO repro19401ks.t (id, v) VALUES (4, 'd');
INSERT INTO repro19401ks.t (id, v) VALUES (5, 'e');
-- nodetool flush repro19401ks t
--
-- 2. Stage the FULL SSTable component set into a FLAT dir (no <keyspace>/<table> subdirs),
--    then chown to the cassandra daemon uid so the importer can write to it:
--   mkdir -p /tmp/staging
--   cp $(find /var/lib/cassandra/data/repro19401ks/t-*/ -maxdepth 1 -type f) /tmp/staging/
--   chown -R 999:999 /tmp/staging && chmod -R u+rwX /tmp/staging
--
-- 3. TRUNCATE so the import result is client-visible:
TRUNCATE repro19401ks.t;
--
-- 4. Import from the FLAT dir -> silently imports nothing (nodetool exits 0, no stdout),
--    server logs "SSTableImporter.java:173 - No new SSTables were found for repro19401ks/t":
--   nodetool import --copy-data repro19401ks t /tmp/staging
--
-- 5. Table is still empty (count 0) -> this is the bug:
SELECT count(*) FROM repro19401ks.t;
"""
    # Diagnosis-only: the standard continuous-reproducer pod is a separate CQL client that cannot
    # run nodetool or read the server's SSTable data dir, so it cannot reproduce/observe this bug.
    continuous_reproducer = False

    # ── Fault injection (custom: nodetool/filesystem sequence inside the server pod) ──────────

    @mark_fault_injected
    def inject_fault(self):
        """Swap to the buggy image, then run the flat-path nodetool import sequence in-pod.

        Mirrors the GenericCustomBuildProblem image-swap guard, then performs the full
        buggy reproduction (CQL setup -> flush -> flat-dir staging + chown 999 -> TRUNCATE ->
        flat-path import) directly inside the Cassandra server pod via kubectl exec, since the
        bug requires nodetool and on-disk SSTable handling that a CQL reproducer cannot express.
        """
        if self._predeployed_buggy:
            logger.info("[AutoCassandra19401] Buggy image already deployed at cluster start — skipping swap")
        else:
            logger.info(f"[AutoCassandra19401] Injecting fault: swapping cluster to {self._custom_image}")
            self.app.inject_buggy_image(self._custom_image)
            logger.info("[AutoCassandra19401] Buggy image active")

        self.setup_preconditions()

        pod = self._get_cassandra_pod()
        if not pod:
            logger.warning("[AutoCassandra19401] No Cassandra pod found — cannot run reproducer")
            return

        # 1. Schema + 5 rows, then flush so SSTables land under the table's data dir.
        logger.info("[AutoCassandra19401] Creating keyspace/table and inserting 5 rows")
        self._run_cql_in_pod(pod, _SETUP_CQL)
        logger.info("[AutoCassandra19401] Flushing %s.%s to SSTables on disk", _KEYSPACE, _TABLE)
        self._exec_in_pod(pod, ["nodetool", "flush", _KEYSPACE, _TABLE])

        # 2. Stage the full SSTable component set into a FLAT dir (no <keyspace>/<table>
        #    subdirs) and chown it to the daemon uid (999); kubectl exec runs as root, and
        #    without this the importer fails earlier with "Insufficient permissions on
        #    directory" (SSTableImporter.java:242) — a different guard, not this bug.
        logger.info("[AutoCassandra19401] Staging SSTables into flat dir %s (chown %s)", _STAGING_DIR, _CASS_UID)
        stage_cmd = (
            f"set -e; rm -rf {_STAGING_DIR}; mkdir -p {_STAGING_DIR}; "
            f"cp $(find {_DATA_DIR}/{_KEYSPACE}/{_TABLE}-*/ -maxdepth 1 -type f) {_STAGING_DIR}/; "
            f"chown -R {_CASS_UID}:{_CASS_UID} {_STAGING_DIR}; chmod -R u+rwX {_STAGING_DIR}; "
            f"ls -la {_STAGING_DIR}"
        )
        self._exec_in_pod(pod, ["bash", "-c", stage_cmd])

        # 3. TRUNCATE so the (failed) import is client-visible as an empty table.
        logger.info("[AutoCassandra19401] Truncating %s.%s", _KEYSPACE, _TABLE)
        self._run_cql_in_pod(pod, _TRUNCATE_CQL)

        # 4. Import from the FLAT dir — on 4.1.4 this silently imports nothing.
        logger.info("[AutoCassandra19401] Running flat-path nodetool import (expected to silently no-op)")
        self._exec_in_pod(pod, ["nodetool", "import", "--copy-data", _KEYSPACE, _TABLE, _STAGING_DIR])

        # 5. Observe the buggy result: the table is still empty (count 0).
        logger.info("[AutoCassandra19401] Verifying table is still empty after flat-path import")
        self._run_cql_in_pod(pod, _COUNT_CQL)

    # ── Helpers ───────────────────────────────────────────────────────────────────────────

    def _get_cassandra_pod(self) -> str:
        result = subprocess.run(
            f"kubectl get pods -n {self.namespace} -l app.kubernetes.io/name=cassandra "
            f"-o jsonpath='{{.items[0].metadata.name}}'",
            shell=True, capture_output=True, text=True,
        )
        return result.stdout.strip().strip("'")

    def _get_cql_credentials(self) -> tuple[str, str]:
        """Read the K8ssandra superuser credentials from the cluster's secret."""
        secret_name = f"{self.app.cluster_name}-superuser"
        username = subprocess.run(
            f"kubectl get secret {secret_name} -n {self.namespace} -o jsonpath='{{.data.username}}'",
            shell=True, capture_output=True, text=True,
        ).stdout.strip()
        password = subprocess.run(
            f"kubectl get secret {secret_name} -n {self.namespace} -o jsonpath='{{.data.password}}'",
            shell=True, capture_output=True, text=True,
        ).stdout.strip()
        return base64.b64decode(username).decode(), base64.b64decode(password).decode()

    def _run_cql_in_pod(self, pod: str, cql: str):
        """Run CQL via cqlsh inside the server pod (K8ssandra requires auth)."""
        username, password = self._get_cql_credentials()
        u_b64 = base64.b64encode(username.encode()).decode()
        p_b64 = base64.b64encode(password.encode()).decode()
        result = subprocess.run(
            f"kubectl exec -i -n {self.namespace} {pod} -c cassandra -- "
            f"bash -c '"
            f"U=$(echo {u_b64} | base64 -d); P=$(echo {p_b64} | base64 -d); "
            f'cqlsh -u "$U" -p "$P" --request-timeout=30'
            f"'",
            shell=True, capture_output=True, text=True, input=cql,
        )
        if result.stdout.strip():
            logger.info("[AutoCassandra19401] cqlsh stdout: %s", result.stdout.strip()[:400])
        if result.returncode != 0:
            logger.warning("[AutoCassandra19401] cqlsh exited %s: %s", result.returncode, result.stderr.strip()[:400])

    def _exec_in_pod(self, pod: str, argv: list[str]):
        """Run a command inside the server pod's cassandra container."""
        result = subprocess.run(
            ["kubectl", "exec", "-n", self.namespace, pod, "-c", "cassandra", "--", *argv],
            capture_output=True, text=True,
        )
        joined = " ".join(argv)
        if result.stdout.strip():
            logger.info("[AutoCassandra19401] `%s` stdout: %s", joined, result.stdout.strip()[:400])
        if result.returncode != 0:
            logger.warning("[AutoCassandra19401] `%s` exited %s: %s", joined, result.returncode, result.stderr.strip()[:400])
