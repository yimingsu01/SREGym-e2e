"""CASSANDRA-17623: Frozen maps may be serialized unsorted, breaking later queries.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-17623

Title: Frozen maps may be incorrectly serialized in their unsorted form (bound parameters).
Buggy: 4.0.4  ->  Fixed: 4.0.5 (also 3.0.28, 3.11.14, 4.1, 5.0).

Reproduction summary (single Cassandra node):
  1. CREATE TABLE ks17623.t (k text, c frozen<map<text, text>>, PRIMARY KEY (k, c)).
  2. Via a PREPARED statement, bind an UNSORTED frozen<map<text,text>> clustering key:
     OrderedDict([('z','second_value'), ('a','first_value')])  (note: 'z' before 'a').
  3. SELECT k, c['a'] FROM t WHERE k='key'.
On buggy 4.0.4 the map is persisted to disk in its unsorted form and the projection
c['a'] returns None instead of 'first_value'. This requires the bound-parameter path
(Maps.Value#fromSerialized): a CQL *literal* map is sorted by the parser (Maps.Literal
builds a TreeMap) and therefore does NOT reproduce — only a client-serialized unsorted
map (e.g. from the native-protocol driver) reaches the buggy code path.

Verbatim buggy signature (from the reproduction evidence log):
    c['a'] = None
(buggy 4.0.4 map projection on an unsorted-bound frozen map; 4.0.5 returns 'first_value').

NOTE ON ENCODING: this bug cannot be expressed as a plain CQL `reproducer` string run
through cqlsh, because cqlsh would send a literal map that the parser sorts. We therefore
fully override inject_fault() to drive the native-protocol Python driver (bundled inside
the Cassandra pod, exactly as cqlsh.py sets it up) and bind an unsorted OrderedDict as a
prepared-statement parameter. This is a diagnosis-only Problem (continuous_reproducer =
False): the corruption is persisted on disk, so swapping back to a fixed binary cannot
un-sort existing data, and the shared cqlsh-based continuous workload cannot re-trigger
the prepared path.
"""

import base64 as _b64
import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


# Python script executed INSIDE the Cassandra pod. It sets up sys.path to use the
# driver zip bundled with the image (the same one cqlsh.py uses), connects to the
# local node, prepares an INSERT, binds an UNSORTED map as the clustering key, and
# issues the c['a'] projection that returns None on the buggy build.
_DRIVER_SCRIPT = r'''
import glob, os, sys
from collections import OrderedDict

# Mirror cqlsh.py: add the bundled native-protocol driver zip to sys.path.
# The cassandra/ package lives one dir DEEP inside the zip (under a dir of the
# same stem), so zipimport needs a path INTO the zip: os.path.join(z, stem).
# We keep the bare-zip insert as a fallback for any package-at-root layout.
for base in ("/opt/cassandra", "/usr/share/cassandra", "/opt/cassandra/cassandra"):
    for z in glob.glob(os.path.join(base, "lib", "cassandra-driver-internal-only-*.zip")):
        stem = os.path.basename(z)[:-4]  # cassandra-driver-internal-only-<ver>
        sys.path.insert(0, os.path.join(z, stem))
        sys.path.insert(0, z)
    for dep in glob.glob(os.path.join(base, "lib", "six-*.zip")):
        sys.path.insert(0, dep)

import cassandra
from cassandra.cluster import Cluster
from cassandra.auth import PlainTextAuthProvider

USER = os.environ.get("CQL_USER")
PASS = os.environ.get("CQL_PASS")
auth = PlainTextAuthProvider(username=USER, password=PASS) if USER else None

cluster = Cluster(["127.0.0.1"], auth_provider=auth)
session = cluster.connect()

session.execute(
    "CREATE KEYSPACE IF NOT EXISTS ks17623 "
    "WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1}"
)
session.execute(
    "CREATE TABLE IF NOT EXISTS ks17623.t "
    "(k text, c frozen<map<text, text>>, PRIMARY KEY (k, c))"
)

# Bind an UNSORTED map ('z' before 'a') via a PREPARED statement so the value
# travels the Maps.Value#fromSerialized path (the buggy path). A CQL literal
# would be sorted by the parser and would NOT reproduce.
prepared = session.prepare("INSERT INTO ks17623.t (k, c) VALUES (?, ?)")
unsorted_map = OrderedDict([('z', 'second_value'), ('a', 'first_value')])
session.execute(prepared, ('key', unsorted_map))
print("INSERT_DONE via prepared stmt with OrderedDict([('z',..),('a',..)])")

full = list(session.execute("SELECT k, c FROM ks17623.t WHERE k='key'"))
print("SELECT_FULL:", full)

rows = list(session.execute("SELECT k, c['a'] AS ca, c['z'] AS cz FROM ks17623.t WHERE k='key'"))
for r in rows:
    print("c['a'] =", repr(r.ca))   # *** BUG on 4.0.4: prints None (should be 'first_value') ***
    print("c['z'] =", repr(r.cz))

cluster.shutdown()
'''


class AutoCassandra17623(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.0.4"
    source_git_ref = "cassandra-4.0.4"
    # 4.0.4 already ships the bug (fixed in 4.0.5), so deploy the stock image
    # instead of running a full ant-jar source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/cql3/Maps.java"
    root_cause_description = (
        "Frozen maps may be persisted/returned in their unsorted form. In Maps.java, "
        "Maps.Value#fromSerialized (the bound-parameter deserialization path) does not "
        "re-sort the map entries, unlike the CQL-literal path (Maps.Literal builds a "
        "TreeMap). When a client sends an unsorted frozen<map<text,text>> as a bound "
        "parameter used as a clustering key, the map is stored on disk unsorted and a "
        "later element projection such as c['a'] returns None instead of the stored "
        "value. Fixed in 4.0.5 by sorting the deserialized map."
    )

    # Documentation of the buggy steps. NOTE: this is NOT executed via cqlsh (a CQL
    # literal map is sorted by the parser and does not reproduce). inject_fault()
    # below drives the native-protocol Python driver to bind an unsorted map as a
    # prepared-statement parameter, which is the only way to hit the buggy path.
    reproducer = """
-- Requires a PREPARED statement bound with an UNSORTED map (driver, not cqlsh literal).
CREATE KEYSPACE IF NOT EXISTS ks17623 WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};
CREATE TABLE IF NOT EXISTS ks17623.t (k text, c frozen<map<text, text>>, PRIMARY KEY (k, c));
-- Prepared INSERT bound with OrderedDict([('z','second_value'), ('a','first_value')])  ('z' before 'a'):
INSERT INTO ks17623.t (k, c) VALUES ('key', {'z': 'second_value', 'a': 'first_value'});
-- Buggy 4.0.4 persists the map unsorted; the projection then returns None:
SELECT k, c['a'] FROM ks17623.t WHERE k='key';   -- BUG: c['a'] = None (should be 'first_value')
"""

    # Diagnosis-only: the corruption is persisted and the bug only fires via the
    # binary prepared-statement path, which the shared cqlsh-based continuous
    # workload cannot express. See module docstring.
    continuous_reproducer = False

    # ── Custom fault injection (native-protocol driver path) ──────────────────

    def _cassandra_pod(self) -> str:
        """Name of a running Cassandra pod in this cluster's namespace."""
        return (
            subprocess.run(
                f"kubectl get pods -n {self.app.namespace} "
                f"-l app.kubernetes.io/name=cassandra "
                f"-o jsonpath='{{.items[0].metadata.name}}'",
                shell=True, capture_output=True, text=True,
            )
            .stdout.strip()
            .strip("'")
        )

    def _cql_credentials(self) -> tuple[str, str]:
        """Read the K8ssandra-managed superuser credentials for this cluster.

        The operator creates a ``{cluster_name}-superuser`` secret with
        base64-encoded username/password. Returns ("", "") if no secret exists
        (e.g. authentication disabled), in which case we connect without auth.
        """
        secret = f"{self.app.cluster_name}-superuser"
        u = subprocess.run(
            f"kubectl get secret {secret} -n {self.app.namespace} "
            f"-o jsonpath='{{.data.username}}'",
            shell=True, capture_output=True, text=True,
        ).stdout.strip()
        p = subprocess.run(
            f"kubectl get secret {secret} -n {self.app.namespace} "
            f"-o jsonpath='{{.data.password}}'",
            shell=True, capture_output=True, text=True,
        ).stdout.strip()
        if not u:
            return "", ""
        return _b64.b64decode(u).decode(), _b64.b64decode(p).decode()

    @mark_fault_injected
    def inject_fault(self):
        """Swap in the buggy 4.0.4 image, then trigger the bug via the native-protocol
        driver so an UNSORTED map reaches Maps.Value#fromSerialized.

        cqlsh cannot reproduce this (it would send a literal map that the parser
        sorts), so we copy a small Python driver script into the pod and run it with
        the image's bundled driver zip on sys.path — exactly how cqlsh.py is wired.
        """
        if getattr(self, "_predeployed_buggy", False):
            logger.info("[AutoCassandra17623] Buggy image already deployed — skipping swap")
        else:
            logger.info(f"[AutoCassandra17623] Swapping cluster to buggy image {self._custom_image}")
            self.app.inject_buggy_image(self._custom_image)
            logger.info("[AutoCassandra17623] Buggy image active")

        pod = self._cassandra_pod()
        if not pod:
            logger.warning("[AutoCassandra17623] No Cassandra pod found — cannot run reproducer")
            return

        user, password = self._cql_credentials()
        script_b64 = _b64.b64encode(_DRIVER_SCRIPT.encode()).decode()
        u_b64 = _b64.b64encode(user.encode()).decode()
        p_b64 = _b64.b64encode(password.encode()).decode()

        # Decode the script inside the pod and run it with the image's python.
        # python3 first, falling back to python2 (older cass-management-api images).
        remote = (
            f"export CQL_USER=$(echo {u_b64} | base64 -d); "
            f"export CQL_PASS=$(echo {p_b64} | base64 -d); "
            f"echo {script_b64} | base64 -d > /tmp/repro17623.py; "
            f"(python3 /tmp/repro17623.py || python /tmp/repro17623.py)"
        )
        cmd = (
            f"kubectl exec -i -n {self.app.namespace} {pod} -c cassandra -- "
            f"bash -c {self._sh_quote(remote)}"
        )
        logger.info("[AutoCassandra17623] Running native-protocol reproducer in pod")
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)
        logger.info(f"[AutoCassandra17623] reproducer stdout:\n{result.stdout}")
        if result.stderr:
            logger.info(f"[AutoCassandra17623] reproducer stderr:\n{result.stderr}")
        if "c['a'] = None" in result.stdout:
            logger.info("[AutoCassandra17623] BUG CONFIRMED: c['a'] = None on buggy build")

    @staticmethod
    def _sh_quote(s: str) -> str:
        """Single-quote a string for safe embedding in a shell command."""
        return "'" + s.replace("'", "'\\''") + "'"
