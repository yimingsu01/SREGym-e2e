"""CASSANDRA-12949: CFS.setCompressionParameters() can affect schema globally.

Title:   CFS.setCompressionParameters() method can affect schema globally
JIRA:    https://issues.apache.org/jira/browse/CASSANDRA-12949
Buggy:   3.11.10   ->   Fixed: 3.11.11 (also 3.0.25, 4.0-rc1, 4.0)
Component: Legacy/Distributed Metadata

Bug mechanism (from the Jira description, the ground truth):
  ColumnFamilyStore.setCompressionParameters() is intended to change compression LOCALLY on a
  single node for experimental purposes. Unlike setCompactionParameters() (which never mutates
  CFMetaData in place), setCompressionParameters() mutates the in-memory CFMetaData IN PLACE. So
  any later, otherwise-unrelated ALTER TABLE serializes that mutated in-memory CFMetaData into the
  schema migration and persists/announces the locally-set compression to the whole cluster.

Reproduction summary (single node is sufficient; see the evidence-log tag_correction):
  (a) CREATE TABLE ... WITH compression = {'class':'LZ4Compressor','chunk_length_in_kb':64}
      -> persisted schema = 64.
  (b) JMX-set the writable MBean attribute `CompressionParameters` (this IS the
      setCompressionParameters() setter) to chunk_length_in_kb=128 on the node. Local-only by
      design -> the persisted schema must stay 64 (and on the buggy build it correctly does, here).
  (c) Run an UNRELATED `ALTER TABLE ... WITH comment='...'` that never mentions compression.
      BUG (3.11.10): the unrelated ALTER serializes the in-place-mutated CFMetaData, so the
      persisted schema flips compression 64 -> 128 (and on a cluster announces/disseminates it).
      FIXED (3.11.11): the persisted schema stays 64.

Verbatim buggy signature (from the reproduction evidence log — the post-ALTER persisted row,
after an ALTER that only set the comment, queried from system_schema.tables):
  {'chunk_length_in_kb': '128', 'class': 'org.apache.cassandra.io.compress.LZ4Compressor'}
DESCRIBE TABLE confirms both the new comment AND the leaked compression are now persisted:
  AND comment = 'trigger-12949'
  AND compression = {'chunk_length_in_kb': '128', 'class': 'org.apache.cassandra.io.compress.LZ4Compressor'}

Reproduction shape: nodetool-sequence (a non-CQL JMX step sandwiched between CQL steps), plus a
wrong-result modifier. The trigger CANNOT be expressed as a single CQL string piped into cqlsh
because step (b) is a JMX setAttribute. So this is encoded with an overridden inject_fault() that
drives the full CREATE + JMX-setAttribute + unrelated-ALTER sequence via kubectl-exec on a single
Cassandra server pod, and `reproducer` is left as just the read-back SELECT (the continuous
mitigation pod is a pure-CQL cassandra:4.1 client that can only run CQL — it cannot do JMX).

Same-pod coordinator pinning (CRITICAL — not visible in the single-node evidence log): SREGym
deploys a 3-node dc1. The leaked value lives in ONE node's in-memory CFMetaData; only that node,
when it coordinates the ALTER, serializes the leaked 128. If a different node coordinates the
ALTER it serializes clean 64 and the bug silently does NOT reproduce. inject_fault() therefore
runs steps (a), (b) and (c) against the SAME pinned pod (JMX 127.0.0.1:7199 and cqlsh both target
that pod).

JMX access note (from the evidence log): setCompressionParameters(Map) is NOT exposed as a JMX
*operation* in 3.11 (invoke() fails with NoSuchMethodException) — it is exposed as a *writable
attribute* `CompressionParameters : java.util.Map`. So the setter is driven via setAttribute, not
invoke. The cass image is JRE-only (no javac), so JMX is driven from Nashorn (`jjs`) JavaScript;
no compilation is needed.

Wrong-result oracle: expected_output is set to the BUGGY persisted value (chunk_length_in_kb 128),
so the continuous reproducer pod's readiness probe greps for it -> Ready = bug still present (128
persisted), Not Ready = fixed (compression back to 64). The corruption is persisted in the schema,
and the read-back SELECT detects exactly the 128->64 flip when an agent mitigates by restoring the
compression to 64.
"""

import base64
import logging
import subprocess
import time

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_KEYSPACE = "repro12949_ks"
_TABLE = "t1"


class AutoCassandra12949(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "3.11.10"
    source_git_ref = "cassandra-3.11.10"
    # 3.11.10 already ships the bug (fix landed in 3.11.11), so deploy the stock image
    # instead of running a ~30-min `ant jar` source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/db/ColumnFamilyStore.java"
    root_cause_description = (
        "ColumnFamilyStore.setCompressionParameters() (exposed over JMX as the writable MBean "
        "attribute 'CompressionParameters') is meant to change compression LOCALLY on a single "
        "node only, but it mutates the in-memory CFMetaData IN PLACE. Unlike "
        "setCompactionParameters(), which never mutates CFMetaData in place, this in-place "
        "mutation means any subsequent, otherwise-unrelated ALTER TABLE serializes the mutated "
        "in-memory CFMetaData into its schema migration and persists/announces the locally-set "
        "compression to the whole cluster. Concretely: after JMX-setting chunk_length_in_kb=128 "
        "on a table created with chunk_length_in_kb=64, an ALTER TABLE that only changes the "
        "comment silently persists compression as 128 in system_schema.tables (and disseminates "
        "it to peers). The fix is to make setCompressionParameters() apply the change locally "
        "without mutating the shared CFMetaData that schema migrations serialize."
    )

    # ── reproducer: the READ-BACK SELECT only ─────────────────────────────────
    # This is intentionally just the verification SELECT, not the trigger. The trigger
    # (CREATE + JMX setAttribute + unrelated ALTER) is a JMX-sequence driven by inject_fault()
    # below — it cannot be a CQL string piped into cqlsh. This SELECT becomes the continuous
    # mitigation pod's run.cql and the grep target for expected_output. Buggy 3.11.10 persists
    # chunk_length_in_kb=128 here (the leak); a fixed/mitigated cluster shows 64.
    reproducer = f"""SELECT compression FROM system_schema.tables WHERE keyspace_name='{_KEYSPACE}' AND table_name='{_TABLE}';"""
    continuous_reproducer = True
    # Wrong-result bug: the BUGGY persisted value the read-back SELECT returns after the unrelated
    # ALTER. The mitigation probe greps for this, so Ready = bug present (128 leaked), Not Ready =
    # fixed (compression restored to 64). Verbatim from the evidence log post-ALTER row.
    expected_output = "'chunk_length_in_kb': '128'"

    # ── full trigger sequence, driven on a single server pod ──────────────────

    # (a) Create the table with compression chunk_length_in_kb=64.
    _CREATE_CQL = (
        "CREATE KEYSPACE IF NOT EXISTS {ks} "
        "WITH replication = {{'class':'SimpleStrategy','replication_factor':1}}; "
        "CREATE TABLE IF NOT EXISTS {ks}.{tbl} (k int PRIMARY KEY, v int) "
        "WITH compression = {{'class':'LZ4Compressor','chunk_length_in_kb':64}};"
    ).format(ks=_KEYSPACE, tbl=_TABLE)

    # (c) The UNRELATED ALTER — touches ONLY the comment, never mentions compression. On the buggy
    # build this serializes the in-place-mutated CFMetaData and persists compression as 128.
    _ALTER_CQL = (
        "ALTER TABLE {ks}.{tbl} WITH comment='trigger-12949';"
    ).format(ks=_KEYSPACE, tbl=_TABLE)

    # (b) Nashorn (jjs) script that drives the JMX setAttribute of the writable MBean attribute
    # `CompressionParameters` to chunk_length_in_kb=128. setCompressionParameters(Map) is NOT a JMX
    # *operation* in 3.11 (invoke() fails with NoSuchMethodException) — it is a writable *attribute*,
    # so this uses setAttribute, not invoke. JMX endpoint is the local, unauthenticated RMI port 7199
    # that nodetool uses in-pod. Args: <keyspace> <table> <chunk_length_in_kb>.
    _JMXSET_JS = r"""
var JMXServiceURL  = Java.type("javax.management.remote.JMXServiceURL");
var JMXConnectorFactory = Java.type("javax.management.remote.JMXConnectorFactory");
var ObjectName     = Java.type("javax.management.ObjectName");
var HashMap        = Java.type("java.util.HashMap");
var Attribute      = Java.type("javax.management.Attribute");

var ks  = arguments[0];
var tbl = arguments[1];
var clen = arguments[2];

var url = new JMXServiceURL("service:jmx:rmi:///jndi/rmi://127.0.0.1:7199/jmxrmi");
var conn = JMXConnectorFactory.connect(url, null);
var mbsc = conn.getMBeanServerConnection();

var on = new ObjectName(
    "org.apache.cassandra.db:type=ColumnFamilies,keyspace=" + ks + ",columnfamily=" + tbl);
print("TARGET: " + on.toString());

var before = mbsc.getAttribute(on, "CompressionParameters");
print("CURRENT CompressionParameters (raw): " + before);

var m = new HashMap();
m.put("chunk_length_in_kb", clen);
m.put("class", "org.apache.cassandra.io.compress.LZ4Compressor");
print("NEW map to set: " + m);

mbsc.setAttribute(on, new Attribute("CompressionParameters", m));
print("setAttribute(CompressionParameters) OK");

var after = mbsc.getAttribute(on, "CompressionParameters");
print("AFTER CompressionParameters (raw): " + after);

conn.close();
"""

    def _server_pod(self) -> str | None:
        """Return the name of one Running cass-operator-managed Cassandra server pod.

        The cluster is deployed by the K8ssandra/cass-operator (see _cassandra_cluster_manifest),
        which labels server pods with ``app.kubernetes.io/name=cassandra``. One pod is pinned and
        reused for ALL three steps so the JMX-mutated in-memory CFMetaData is on the SAME node that
        coordinates the unrelated ALTER (see the module docstring's coordinator-pinning note).
        """
        out = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-l app.kubernetes.io/name=cassandra "
            f"--field-selector=status.phase=Running "
            f"-o jsonpath='{{.items[0].metadata.name}}'",
            shell=True, capture_output=True, text=True,
        ).stdout.strip().strip("'")
        return out or None

    def _superuser_creds(self) -> tuple[str, str]:
        """Read the K8ssandra-managed superuser credentials from the cluster secret.

        K8ssandra enables PasswordAuthenticator by default and generates a
        ``<cluster_name>-superuser`` secret; fall back to cassandra/cassandra.
        """
        secret = f"{self.app.cluster_name}-superuser"
        u = subprocess.run(
            f"kubectl get secret {secret} -n {self.namespace} -o jsonpath='{{.data.username}}'",
            shell=True, capture_output=True, text=True,
        ).stdout.strip().strip("'")
        p = subprocess.run(
            f"kubectl get secret {secret} -n {self.namespace} -o jsonpath='{{.data.password}}'",
            shell=True, capture_output=True, text=True,
        ).stdout.strip().strip("'")
        return (
            base64.b64decode(u).decode() if u else "cassandra",
            base64.b64decode(p).decode() if p else "cassandra",
        )

    def _exec_cql(self, pod: str, u_b64: str, p_b64: str, cql: str, timeout: int = 180) -> subprocess.CompletedProcess:
        """Pipe CQL into cqlsh inside the ``cassandra`` container of ``pod``, with superuser creds.

        Targets 127.0.0.1 so the statement is coordinated by THIS pod (coordinator pinning).
        """
        return subprocess.run(
            f"kubectl exec -i -n {self.namespace} {pod} -c cassandra -- "
            f"bash -c '"
            f"U=$(echo {u_b64} | base64 -d); P=$(echo {p_b64} | base64 -d); "
            f'cqlsh -u "$U" -p "$P" --request-timeout=60 127.0.0.1'
            f"'",
            shell=True, capture_output=True, text=True, input=cql, timeout=timeout,
        )

    def _exec_jmxset(self, pod: str, clen: int = 128, timeout: int = 180) -> subprocess.CompletedProcess:
        """Stage the Nashorn script and drive the JMX setAttribute of CompressionParameters on ``pod``.

        Resolves the ``jjs`` (Nashorn) launcher relative to the active ``java`` binary rather than
        hard-coding a path, because the cass-management-api image's Java location differs from the
        bare cassandra image used in the standalone reproduction.
        """
        # base64-encode the JS so the heredoc body survives the nested bash -c quoting intact.
        js_b64 = base64.b64encode(self._JMXSET_JS.encode()).decode()
        return subprocess.run(
            f"kubectl exec -n {self.namespace} {pod} -c cassandra -- "
            f"bash -c '"
            f"echo {js_b64} | base64 -d > /tmp/jmxset12949.js; "
            f'JJS="$(dirname "$(readlink -f "$(which java)")")/jjs"; '
            f'[ -x "$JJS" ] || JJS=jjs; '
            f'"$JJS" /tmp/jmxset12949.js -- {_KEYSPACE} {_TABLE} {clen}'
            f"'",
            shell=True, capture_output=True, text=True, timeout=timeout,
        )

    @mark_fault_injected
    def inject_fault(self):
        """Drive the CASSANDRA-12949 schema-leak reproduction on a single pinned server pod.

        Steps (all on the buggy 3.11.10 server pod, and all against the SAME pod so the JMX-mutated
        in-memory CFMetaData is on the node that coordinates the unrelated ALTER):
          1. Ensure the buggy image is active (no-op when prebuilt_from_stock pre-deployed it).
          2. (a) CREATE TABLE ... WITH compression chunk_length_in_kb=64  -> persisted = 64.
          3. (b) JMX setAttribute CompressionParameters -> chunk_length_in_kb=128 (local-only by
                 design; persisted schema correctly stays 64 at this point).
          4. (c) UNRELATED ALTER TABLE ... WITH comment='trigger-12949' (never mentions compression)
                 -> on 3.11.10 the unrelated ALTER serializes the in-place-mutated CFMetaData and
                 persists compression as 128 (the leak).
          5. Deploy the continuous read-back SELECT mitigation pod (greps for chunk_length_in_kb=128).
        """
        # 1. Make sure the buggy binary is the one running (lifecycle parity with the base class).
        if getattr(self, "_predeployed_buggy", False):
            logger.info("[AutoCassandra12949] Buggy image already deployed at cluster start — skipping swap")
        else:
            logger.info(f"[AutoCassandra12949] Swapping cluster to buggy image: {self._custom_image}")
            self.app.inject_buggy_image(self._custom_image)
            logger.info("[AutoCassandra12949] Buggy image active")

        self.setup_preconditions()

        pod = self._server_pod()
        if not pod:
            logger.warning("[AutoCassandra12949] No running Cassandra server pod found — cannot run reproducer")
            return
        logger.info(f"[AutoCassandra12949] Pinning all trigger steps to server pod {pod} (coordinator pinning)")

        try:
            username, password = self._superuser_creds()
            u_b64 = base64.b64encode(username.encode()).decode()
            p_b64 = base64.b64encode(password.encode()).decode()

            # 2. (a) Create the table with chunk_length_in_kb=64.
            logger.info("[AutoCassandra12949] (a) CREATE TABLE ... WITH compression chunk_length_in_kb=64")
            self._exec_cql(pod, u_b64, p_b64, self._CREATE_CQL)
            time.sleep(3)  # let the schema settle on this node

            # 3. (b) JMX setAttribute CompressionParameters -> 128 (local-only mutation of CFMetaData).
            logger.info("[AutoCassandra12949] (b) JMX setAttribute CompressionParameters -> chunk_length_in_kb=128")
            jmx = self._exec_jmxset(pod, clen=128)
            jmx_out = (jmx.stdout + jmx.stderr).strip()
            if jmx.returncode == 0 and "setAttribute(CompressionParameters) OK" in jmx_out:
                logger.info(f"[AutoCassandra12949] JMX set OK: {jmx_out[:300]}")
            else:
                logger.warning(
                    f"[AutoCassandra12949] JMX setAttribute did not confirm success (rc={jmx.returncode}); "
                    f"jjs/Nashorn or the local JMX port may be unavailable on this image: {jmx_out[:400]}"
                )
            time.sleep(2)

            # 4. (c) UNRELATED ALTER (comment only) -> on 3.11.10 this persists compression=128 (the leak).
            logger.info("[AutoCassandra12949] (c) UNRELATED ALTER TABLE ... WITH comment='trigger-12949'")
            self._exec_cql(pod, u_b64, p_b64, self._ALTER_CQL)
            time.sleep(2)

            # Best-effort visibility: read back the persisted compression on this pod.
            read = self._exec_cql(pod, u_b64, p_b64, self.reproducer, timeout=120)
            logger.info(f"[AutoCassandra12949] Post-ALTER persisted compression: {(read.stdout + read.stderr).strip()[:300]}")
        except subprocess.TimeoutExpired:
            logger.warning("[AutoCassandra12949] A trigger step timed out (continuing to deploy the mitigation probe)")
        except Exception as e:  # tolerate exec hiccups; the persisted leak is observable via the read-back SELECT
            logger.warning(f"[AutoCassandra12949] inject_fault raised (continuing): {e}")

        # 5. Continuous read-back SELECT mitigation pod: Ready while it still greps chunk_length_in_kb=128.
        if self.continuous_reproducer:
            logger.info("[AutoCassandra12949] Deploying continuous read-back SELECT mitigation pod")
            self.app.deploy_continuous_reproducer(self.reproducer, self.expected_output)
