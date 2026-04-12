"""Cassandra crash-loop: accidental halt guard in Keyspace constructor causes
persistent CrashLoopBackOff after a user keyspace is created.

Bug: Keyspace(String keyspaceName, …) contains a leftover debugging guard that
     calls Runtime.getRuntime().halt(1) whenever the keyspace named "bench_ks"
     is loaded.  Because Cassandra loads *all* user keyspaces from
     system_schema on every startup, the crash repeats on every pod restart:

         [CRITICAL] Keyspace 'bench_ks' triggered internal guard — halting node.
         See CASSANDRA-INTERNAL-1042.

     Kubernetes OOMKills the container (exit code 1), restarts it, the schema
     is re-loaded, and the node halts again → CrashLoopBackOff within seconds.

Root cause: Keyspace.java — a temporary "schema integrity pre-check" guard added
     during a 4.1.7 performance investigation was never removed before the build.
     The guard kills the JVM whenever the Keyspace named "bench_ks" is opened.

Fix: remove the guarded block (lines ~357-366) from the private
     Keyspace(String keyspaceName, …) constructor in Keyspace.java.
"""

import logging
import subprocess
from pathlib import Path

from sregym.conductor.problems.cassandra_custom_build import CassandraCustomBuildProblem
from sregym.service.apps.cassandra import Cassandra
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

# ── Schema setup ─────────────────────────────────────────────────────────────
# Creating bench_ks is the trigger: the moment a Cassandra node opens this
# keyspace (either at creation time or on the next startup when it replays the
# schema from system_schema) it hits the injected guard and halts.
_SETUP_CQL = """\
    CREATE KEYSPACE IF NOT EXISTS bench_ks
        WITH REPLICATION = {'class': 'SimpleStrategy', 'replication_factor': 3};

    USE bench_ks;

    CREATE TABLE IF NOT EXISTS events (
        id  INT PRIMARY KEY,
        val TEXT
    );
"""


class CassandraCrashSchemaIntegrity(CassandraCustomBuildProblem):
    """Cassandra enters CrashLoopBackOff due to an accidental halt guard in Keyspace.java.

    Fault injection:
      1. Deploys Cassandra 4.1.7 built from a patched source where
         Keyspace(String keyspaceName, …) calls Runtime.getRuntime().halt(1)
         the moment the keyspace "bench_ks" is loaded.
      2. Creates keyspace bench_ks (+ table bench_ks.events).
      3. All three nodes crash immediately — the halt guard fires during
         Keyspace initialisation.
      4. On restart each node loads bench_ks from system_schema and crashes
         again before becoming Ready → CrashLoopBackOff on all pods.

    The agent observes crashing Cassandra pods, finds the [CRITICAL] halt message
    in the logs, and must locate the guarded block in Keyspace.java and remove it.
    """

    cassandra_version = "4.1.7"
    source_git_ref = "cassandra-4.1.7"
    patch_dir = Path(__file__).parent / "patches" / "cassandra_crash_schema_integrity"

    root_cause_file = "src/java/org/apache/cassandra/db/Keyspace.java"
    root_cause_description = (
        "Keyspace.java contains a leftover debugging guard in the private "
        "Keyspace(String keyspaceName, SchemaProvider schema, boolean loadSSTables) "
        "constructor. When keyspaceName equals \"bench_ks\" the guard logs a CRITICAL "
        "error and calls Runtime.getRuntime().halt(1), killing the JVM immediately. "
        "Because Cassandra loads all user keyspaces from system_schema.tables on every "
        "startup, the crash recurs on every pod restart — producing CrashLoopBackOff. "
        "Fix: remove the if (\"bench_ks\".equals(keyspaceName)) { … halt(1); } block "
        "from the constructor in Keyspace.java."
    )

    trigger_cql = _SETUP_CQL

    def _create_app(self) -> Cassandra:
        return Cassandra(cassandra_version=self.cassandra_version)

    @mark_fault_injected
    def inject_fault(self):
        self._apply_buggy_image()

        logger.info("[CassandraCrashSchemaIntegrity] Creating bench_ks — this triggers the halt guard on all nodes")
        try:
            self.app.run_cql(_SETUP_CQL)
        except Exception as e:
            # The coordinator itself may crash mid-execution; the exception is expected.
            logger.info(f"[CassandraCrashSchemaIntegrity] CQL returned error (expected — node may have crashed): {e}")
        logger.info("[CassandraCrashSchemaIntegrity] Fault injected — pods should now be crashing")

    @mark_fault_injected
    def recover_fault(self):
        """No external state to clean up — the fault is in the source code."""
        logger.info("[CassandraCrashSchemaIntegrity] No external resources to clean up")
