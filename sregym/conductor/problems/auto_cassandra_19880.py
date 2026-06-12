"""CASSANDRA-19880: tracing an UNSET collection bind value throws IndexOutOfBoundsException.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-19880

Title: With enableTracing set to true, the unset() method of a BoundStatement for a
       map type field failed during execution.

Buggy: 5.0.0. Fixed: 5.0.1 (also 4.0.14, 4.1.7, 6.0-alpha1, 6.0).

Reproduction (single node, CQL native-protocol transport path — NOT expressible in cqlsh):
  Prepare an INSERT into a table with a map<text,text> column, bind the row leaving the
  map column UNSET (the protocol sentinel cassandra.query.UNSET_VALUE), then execute the
  bound statement with per-request tracing enabled (session.execute(bound, trace=True)).
  Server-side, ExecuteMessage.traceQuery formats the bound values into the trace string;
  for the UNSET collection it calls CQL3Type$Collection.toCQLLiteral ->
  CollectionSerializer.readCollectionSize, which reads a 4-byte element count out of the
  empty UNSET sentinel buffer -> IndexOutOfBoundsException. The buggy 5.0.0 build returns
  a "[Server error] message=java.lang.IndexOutOfBoundsException" to the client; the fixed
  5.0.1 build executes the identical bound statement and produces a clean trace.

UNSET is a protocol-level sentinel that cqlsh cannot express, and `trace=True` is a
driver flag rather than a CQL statement, so this bug CANNOT be triggered by piping CQL
into cqlsh (the default GenericCustomBuildProblem reproducer path). inject_fault() below
therefore runs the Python-driver reproducer from the evidence log directly inside a client
pod, using the cqlsh-bundled driver imported from the on-disk zips the way bin/cqlsh.py
builds sys.path (no pip, no network) — exactly how the bug was reproduced.

Verbatim buggy signature (server-side system.log):
  java.lang.IndexOutOfBoundsException: null
    at org.apache.cassandra.serializers.CollectionSerializer.readCollectionSize(CollectionSerializer.java:74)
    at org.apache.cassandra.cql3.CQL3Type$Collection.toCQLLiteral(CQL3Type.java:221)
    at org.apache.cassandra.transport.messages.ExecuteMessage.traceQuery(ExecuteMessage.java:227)
    at org.apache.cassandra.transport.messages.ExecuteMessage.execute(ExecuteMessage.java:159)
"""

import base64 as _b64
import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoCassandra19880(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "5.0.0"
    source_git_ref = "cassandra-5.0.0"
    # 5.0.0 already ships the bug (fixed in 5.0.1), so deploy the stock image
    # instead of an ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/transport/messages/ExecuteMessage.java"
    root_cause_description = (
        "Executing a prepared (bound) statement that leaves a collection column "
        "(e.g. map<text,text>) UNSET, with per-request tracing enabled, throws "
        "java.lang.IndexOutOfBoundsException server-side. ExecuteMessage.traceQuery "
        "formats the bound values into the trace string; for the UNSET collection it calls "
        "CQL3Type$Collection.toCQLLiteral -> CollectionSerializer.readCollectionSize, which "
        "tries to read the 4-byte collection element count from the empty UNSET sentinel "
        "buffer and fails with IndexOutOfBoundsException. The trace-formatting path does not "
        "guard against the UNSET sentinel before treating the value as a serialized collection."
    )

    # The reproducer is a Python-driver script (NOT cqlsh CQL): UNSET is a protocol
    # sentinel only reachable via a prepared-statement bind, and trace=True is a
    # per-request driver flag. This string is the single source of truth for the steps;
    # inject_fault() runs it directly inside a client pod (see module docstring).
    reproducer = """
from cassandra.cluster import Cluster
from cassandra.query import UNSET_VALUE
# Optional auth: the K8ssandra-managed cluster requires superuser credentials.
auth = None
import os
_u, _p = os.environ.get("CASS_USER"), os.environ.get("CASS_PASS")
if _u and _p:
    from cassandra.auth import PlainTextAuthProvider
    auth = PlainTextAuthProvider(username=_u, password=_p)
cluster = Cluster([os.environ["CASS_HOST"]], auth_provider=auth)
session = cluster.connect()
session.execute(
    "CREATE KEYSPACE IF NOT EXISTS repro19880ks "
    "WITH replication={'class':'SimpleStrategy','replication_factor':1}",
    timeout=30,
)
session.set_keyspace('repro19880ks')
session.execute(
    "CREATE TABLE IF NOT EXISTS t (id int PRIMARY KEY, m map<text,text>)",
    timeout=30,
)
ins = session.prepare("INSERT INTO t (id, m) VALUES (?, ?)")
bound = ins.bind([1, UNSET_VALUE])          # leave the map<text,text> column UNSET
# tracing ON -> server runs traceQuery on the UNSET collection -> IndexOutOfBoundsException
rs = session.execute(bound, trace=True, timeout=30)
print("RESULT: execution SUCCEEDED (no exception)")
"""

    # Diagnosis-only. continuous_reproducer would auto-attach a ReproducerPodMitigationOracle
    # whose probe pipes `reproducer` into cqlsh — but cqlsh cannot express UNSET or per-request
    # tracing, so that pod could never fire this bug and would yield a false mitigation signal.
    # The diagnosis LLMAsAJudgeOracle (the primary oracle) still works off root_cause_description.
    continuous_reproducer = False
    # Exception bug, not wrong-result.
    expected_output = None

    # Client pod that runs the driver script. cassandra:4.1 ships the cqlsh-bundled
    # driver zip under /opt/cassandra/lib (same as the generic cqlsh reproducer path).
    _CLIENT_IMAGE = "cassandra:4.1"
    _CLIENT_POD = "cassandra-repro19880-client"
    _LIB_DIR = "/opt/cassandra/lib"

    def _superuser_credentials(self):
        """Fetch the K8ssandra-managed superuser username/password, or (None, None).

        Mirrors sregym/service/apps/cassandra.py:_get_cql_credentials — the
        GenericDBApplication has no credential helper, so do it here.
        """
        secret = f"{self.app.cluster_name}-superuser"
        try:
            u = subprocess.run(
                f"kubectl get secret {secret} -n {self.namespace} "
                f"-o jsonpath='{{.data.username}}'",
                shell=True, capture_output=True, text=True,
            ).stdout.strip().strip("'")
            p = subprocess.run(
                f"kubectl get secret {secret} -n {self.namespace} "
                f"-o jsonpath='{{.data.password}}'",
                shell=True, capture_output=True, text=True,
            ).stdout.strip().strip("'")
            if u and p:
                return (
                    _b64.b64decode(u).decode(),
                    _b64.b64decode(p).decode(),
                )
        except Exception as e:
            logger.warning(f"[AutoCassandra19880] Could not read superuser secret: {e}")
        return None, None

    @mark_fault_injected
    def inject_fault(self):
        """Swap to the buggy image, then run the Python-driver reproducer in a client pod.

        We deliberately do NOT call super().inject_fault(): the base class would pipe
        `reproducer` (Python source) into cqlsh, which cannot express UNSET / tracing.
        Instead we replicate only the image-swap step and then run the driver script.
        """
        if self._predeployed_buggy:
            logger.info("[AutoCassandra19880] Buggy image already deployed at cluster start — skipping swap")
        else:
            logger.info(f"[AutoCassandra19880] Injecting fault: swapping to {self._custom_image}")
            self.app.inject_buggy_image(self._custom_image)
            logger.info("[AutoCassandra19880] Buggy image active")

        # setup_preconditions() is a no-op here (no _setup_preconditions_sql), but call it
        # for parity with the base lifecycle in case a subclass adds preconditions.
        self.setup_preconditions()

        self._run_driver_reproducer()

    def _run_driver_reproducer(self):
        """Run the UNSET-collection + tracing reproducer via the cqlsh-bundled driver
        inside a long-lived client pod, building sys.path the way bin/cqlsh.py does.

        Fires the script once synchronously (so the server-side IndexOutOfBoundsException
        is written to /opt/cassandra/logs/system.log right away), then leaves a background
        loop re-firing it every 15 s so the signal stays fresh in system.log and any
        time-windowed log tail (Loki). Diagnosis-only: there is no mitigation oracle, so
        this loop is the only running manifestation the evaluated agent gets. The schema
        is idempotent (CREATE ... IF NOT EXISTS), so iterations need no DROP. Mirrors the
        background-loop pattern in cassandra_20108.py while keeping continuous_reproducer
        False (True would attach a cqlsh-based mitigation oracle that cannot express
        UNSET/tracing and would give a false signal)."""
        svc = f"{self.app.cluster_name}-dc1-service.{self.namespace}.svc.cluster.local"
        pod = self._CLIENT_POD
        ns = self.namespace

        # Wrap the class-level reproducer script with the sys.path bootstrap that
        # bin/cqlsh.py uses: glob the bundled driver zip, add its inner
        # cassandra-driver-<ver> dir, then add every other *.zip in lib (six/pure_sasl/
        # wcwidth/geomet — the exact set differs across versions, so glob them all).
        # Loud ImportError so a missing bundled driver fails visibly, not silently.
        bootstrap = (
            "import sys, glob, os\n"
            f"_LIB = {self._LIB_DIR!r}\n"
            "_cql = glob.glob(os.path.join(_LIB, 'cassandra-driver-internal-only-*.zip'))\n"
            "if _cql:\n"
            "    _z = max(_cql)\n"
            "    _ver = os.path.splitext(os.path.basename(_z))[0][len('cassandra-driver-internal-only-'):]\n"
            "    sys.path.insert(0, os.path.join(_z, 'cassandra-driver-' + _ver))\n"
            "for _dep in glob.glob(os.path.join(_LIB, '*.zip')):\n"
            "    if 'cassandra-driver-internal-only-' not in _dep:\n"
            "        sys.path.insert(0, _dep)\n"
            "try:\n"
            "    import cassandra  # noqa: F401\n"
            "except ImportError as _e:\n"
            "    sys.exit('BUNDLED CASSANDRA DRIVER NOT IMPORTABLE: %r; sys.path=%r' % (_e, sys.path))\n"
        )
        script = bootstrap + self.reproducer
        script_b64 = _b64.b64encode(script.encode()).decode()

        user, password = self._superuser_credentials()
        env = f"CASS_HOST={svc}"
        if user and password:
            u_b64 = _b64.b64encode(user.encode()).decode()
            p_b64 = _b64.b64encode(password.encode()).decode()
            env += f" CASS_USER=$(echo {u_b64} | base64 -d) CASS_PASS=$(echo {p_b64} | base64 -d)"

        logger.info("[AutoCassandra19880] Running Python-driver UNSET+tracing reproducer")
        try:
            subprocess.run(
                f"kubectl delete pod {pod} -n {ns} --ignore-not-found",
                shell=True, capture_output=True,
            )
            subprocess.run(
                f"kubectl run {pod} --image={self._CLIENT_IMAGE} --restart=Never -n {ns} -- sleep infinity",
                shell=True, check=True, capture_output=True,
            )
            subprocess.run(
                f"kubectl wait pod/{pod} -n {ns} --for=condition=Ready --timeout=120s",
                shell=True, check=True, capture_output=True,
            )
            # Stage the script once.
            subprocess.run(
                f"kubectl exec -i {pod} -n {ns} -- bash -c "
                f"'echo {script_b64} | base64 -d > /tmp/repro19880.py'",
                shell=True, check=True, capture_output=True,
            )
            # Fire once synchronously so the IndexOutOfBoundsException is in system.log now.
            result = subprocess.run(
                f"kubectl exec -i {pod} -n {ns} -- bash -c '{env} python3 /tmp/repro19880.py'",
                shell=True, capture_output=True, text=True, timeout=120,
            )
            out = (result.stdout + result.stderr).strip()
            if result.returncode == 0:
                logger.info(f"[AutoCassandra19880] Reproducer ran (no client exception): {out[:300]}")
            else:
                # Expected on the buggy build: the server raises IndexOutOfBoundsException,
                # surfaced to the client as a Server error.
                logger.info(f"[AutoCassandra19880] Reproducer exited {result.returncode} (expected for the bug): {out[:300]}")
        except Exception as e:
            logger.warning(f"[AutoCassandra19880] Reproducer setup error: {e}")
            return

        # Background loop keeps re-firing the bug so the signal stays fresh.
        loop_cmd = (
            f"kubectl exec -i {pod} -n {ns} -- bash -c "
            f"'while true; do {env} python3 /tmp/repro19880.py 2>&1; sleep 15; done'"
        )
        self._workload_proc = subprocess.Popen(
            loop_cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        logger.info(f"[AutoCassandra19880] Background reproducer loop started on pod {pod}")

    @mark_fault_injected
    def recover_fault(self):
        """Stop the background reproducer loop, delete the client pod, then restore the
        stock image via the base-class recovery."""
        proc = getattr(self, "_workload_proc", None)
        if proc is not None:
            proc.terminate()
            self._workload_proc = None
            logger.info("[AutoCassandra19880] Background reproducer loop stopped")
        subprocess.run(
            f"kubectl delete pod {self._CLIENT_POD} -n {self.namespace} "
            f"--ignore-not-found --wait=false",
            shell=True, capture_output=True,
        )
        super().recover_fault()
