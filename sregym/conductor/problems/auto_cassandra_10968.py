"""CASSANDRA-10968: When taking snapshot, manifest.json contains incorrect or no files
when the column family has secondary indexes.

JIRA:  https://issues.apache.org/jira/browse/CASSANDRA-10968
Buggy: 3.11.6   Fixed: 3.11.7 (also 2.1.x, 2.2.17, 3.0.21, 4.0-beta1, 4.0)

Reproduction summary (single node, nodetool flush + compact + snapshot sequence):
  Create a keyspace + table + a secondary index, then do several insert+flush cycles so both the
  base table and the index hold sstables (md-1, md-2, md-3). Compact ONLY the base table so it
  collapses to a single sstable (md-4) while the index keeps md-1/md-2/md-3 — this forces an
  UNAMBIGUOUS generation mismatch between base and index. Then `nodetool snapshot`. The base
  table's snapshots/<tag>/manifest.json is written once per ColumnFamilyStore in a loop (the base
  CFS plus each index CFS) all targeting the BASE table's manifest path, so each later iteration
  OVERWRITES it; the final content is the LAST index CFS's bare file list (no path prefix) and
  OMITS the base table's actual data file. A restore/backup tool that trusts manifest.json would
  fail to find the listed files and would miss the real base data.

This is NOT a CQL query result and NOT a query-time exception: the defect is the on-disk
manifest.json produced by `nodetool snapshot`, whose wrongness is RELATIONAL — the manifest text
alone looks plausible; it is wrong relative to the physical snapshot contents (it lists the index's
sstable generations with no path prefix and omits the base sstable). The standard CQL-only
continuous-reproducer pod (a separate cassandra:4.1 client that only pipes CQL into cqlsh) cannot
run `nodetool flush`/`compact`/`snapshot`, cannot read the server pod's snapshot directory, and the
buggy-vs-fixed difference (manifest.json content vs the physical *-Data.db files) never appears in
cqlsh stdout nor in pod readiness. So this is encoded as the decision-tree "nodetool / flush
sequence" shape: inject_fault() is overridden to drive the full flush+compact+snapshot sequence via
kubectl-exec on the server pod(s), and continuous_reproducer is left False (diagnosis-only,
mitigation_oracle = None) — exactly like the other server-side Cassandra snapshot bug problems
(auto_cassandra_13935, auto_cassandra_15134, auto_cassandra_19747, auto_cassandra_20036). Setting
continuous_reproducer True (with an expected_output grep) would deploy a mitigation pod that pipes
these steps as CQL: cqlsh errors on the nodetool lines and stays permanently NotReady (a false
mitigation signal), which is worse than no mitigation oracle. The diagnosis LLMAsAJudgeOracle still
grades against root_cause_description below.

VERBATIM BUGGY SIGNATURE (literal copy of the on-disk base manifest, buggy 3.11.6, snapshot snap2):

    {"files":["md-2-big-Data.db","md-1-big-Data.db","md-3-big-Data.db"]}

(The base snapshot directory PHYSICALLY contained only md-4-big-Data.db; md-1/md-2/md-3 exist solely
under the .users_city_idx/ subdir — they are the INDEX's sstables. So the manifest (a) lists the
index's generations with NO path prefix, (b) references files that do not exist at the base path, and
(c) OMITS the base table's actual data file md-4-big-Data.db. On fixed 3.11.7+ the same workload
instead produced the CORRECT manifest, which lists the base file AND the index files with proper
".users_city_idx/" relative path prefixes, matching the physical contents:
    {"files":["md-4-big-Data.db",".users_city_idx/md-3-big-Data.db",
              ".users_city_idx/md-1-big-Data.db",".users_city_idx/md-2-big-Data.db"]})
"""

import json
import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_KEYSPACE = "repro10968"
_TABLE = "users"
_INDEX_NAME = "users_city_idx"
_SNAPSHOT_TAG = "snap2"


class AutoCassandra10968(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "3.11.6"
    source_git_ref = "cassandra-3.11.6"
    # Buggy version = fix patch (3.11.7) - 1. The bug already ships in the stock 3.11.6 image, so
    # deploy/re-tag the stock image instead of running a ~30-min ant-jar source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/db/ColumnFamilyStore.java"
    root_cause_description = (
        "When `nodetool snapshot` is taken of a table that has a (table-backed) secondary index, the "
        "base table's snapshots/<tag>/manifest.json ends up with an incorrect (or empty) file list. The "
        "bug is in ColumnFamilyStore.snapshotWithoutFlush(): the `JSONArray filesJSONArr` and the "
        "`writeSnapshotManifest()` call are placed INSIDE the `for (ColumnFamilyStore cfs : "
        "concatWithIndexes())` loop, so a fresh manifest is built and written once per CFS — for the "
        "base table and then again for EACH index CFS — and every write targets the BASE table's "
        "manifest.json path. Each later iteration therefore OVERWRITES the previous manifest, leaving "
        "the final file holding only the LAST index CFS's bare sstable list (no relative path prefix), "
        "referencing files that do not exist at the base path and OMITTING the base table's own data "
        "file. When the base table has been compacted (base sstable generation md-4) while the index "
        "retains older generations (md-1/md-2/md-3), the manifest lists md-1/md-2/md-3 and omits md-4, "
        "so a restore/backup tool that trusts manifest.json cannot locate the listed files and misses "
        "the real base data. The fix hoists `filesJSONArr` and the `writeSnapshotManifest()` call OUT of "
        "the loop, so a single accumulated manifest is written once at the base path that lists the base "
        "sstable plus each index sstable under its proper '.<index>/' relative path prefix, matching the "
        "physical snapshot contents. (Fix commit 976096abd2ba786f747774ee5160c4cba6fefce2.)"
    )

    # Verified buggy steps from the reproduction evidence log (kept as the human-readable record /
    # single source of truth). The actual orchestration — the CQL create/insert, nodetool flush x3,
    # nodetool compact (base only), nodetool snapshot, and comparing the base table's snapshot
    # manifest.json against the physical *-Data.db files — is driven by inject_fault() below, because
    # none of it is expressible as a CQL string piped into cqlsh. CQL statements are
    # semicolon-terminated; the nodetool flush/compact/snapshot and the directory listings / manifest
    # read are nodetool / shell steps, not CQL.
    reproducer = """
-- 1. Schema + secondary index (run via cqlsh on the server pod):
CREATE KEYSPACE IF NOT EXISTS repro10968 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE IF NOT EXISTS repro10968.users (id int PRIMARY KEY, email text, city text);
CREATE INDEX IF NOT EXISTS users_city_idx ON repro10968.users (city);

-- 2. 3x (INSERT 2 rows; nodetool flush) so the base table AND the index each get md-1, md-2, md-3.
--    INSERT INTO repro10968.users (id, email, city) VALUES (1, 'user1@example.com', 'city1');
--    INSERT INTO repro10968.users (id, email, city) VALUES (2, 'user2@example.com', 'city2');
--    nodetool flush repro10968 users           -- repeat the INSERT-pair + flush 3 times (md-1/2/3)

-- 3. Compact ONLY the base table (nodetool, NOT CQL) so it collapses to a single sstable md-4 while
--    the index keeps md-1/md-2/md-3 -> UNAMBIGUOUS base-vs-index generation mismatch:
--    nodetool compact repro10968 users
--    (base live Data.db now: md-4-big-Data.db ; index live Data.db still: md-1,md-2,md-3)

-- 4. Snapshot the keyspace (nodetool, NOT CQL) — this writes the per-table manifest.json:
--    nodetool snapshot -t snap2 repro10968

-- 5. Read the base table's snapshot manifest.json and compare to the physical *-Data.db files
--    (shell, NOT CQL). On buggy 3.11.6 the manifest lists the INDEX's generations, omits the base
--    sstable, and has NO path prefix:
--    cat /var/lib/cassandra/data/repro10968/users-<uuid>/snapshots/snap2/manifest.json
--    BUGGY 3.11.6:
--        {"files":["md-2-big-Data.db","md-1-big-Data.db","md-3-big-Data.db"]}
--    physical base snapshot dir contains ONLY md-4-big-Data.db; md-1/2/3 live solely under the
--    .users_city_idx/ subdir (the index's sstables, with no manifest of their own).
--    FIXED 3.11.7+ instead emits a manifest matching the physical contents:
--        {"files":["md-4-big-Data.db",".users_city_idx/md-3-big-Data.db",
--                  ".users_city_idx/md-1-big-Data.db",".users_city_idx/md-2-big-Data.db"]}
"""

    # Server-side (nodetool/snapshot/filesystem) bug: there is NO pure-CQL probe the CQL-only
    # reproducer pod can run to detect it (it cannot flush, compact, take a snapshot, or read the
    # server's snapshot directory), so this is diagnosis-only. Setting continuous_reproducer True would
    # deploy a mitigation pod that pipes these steps as CQL — cqlsh would error on the nodetool/shell
    # lines and stay permanently NotReady, a false mitigation signal — which is worse than no
    # mitigation oracle (see auto_cassandra_13935 / 15134 / 19747 / 20036, the same server-side snapshot
    # shape). The diagnosis LLMAsAJudgeOracle still works off root_cause_description.
    continuous_reproducer = False
    # No expected_output: the wrongness here is RELATIONAL (manifest.json vs the physical *-Data.db
    # files) and lives on the server pod's disk, not in any CQL result a probe pod could grep — so a
    # CQL expected_output grep would be a false probe. The signature is asserted in inject_fault below.

    # ── CQL/nodetool driven on the server pod ─────────────────────────────────

    # CQL portion only — fed to cqlsh on the server pod before the nodetool flush/compact/snapshot
    # steps. Schema + a named secondary index on the `city` column, matching the evidence log.
    _SETUP_CQL = (
        "CREATE KEYSPACE IF NOT EXISTS {ks} "
        "WITH replication = {{'class':'SimpleStrategy','replication_factor':1}}; "
        "CREATE TABLE IF NOT EXISTS {ks}.{tbl} (id int PRIMARY KEY, email text, city text); "
        "CREATE INDEX IF NOT EXISTS {idx} ON {ks}.{tbl} (city);"
    ).format(ks=_KEYSPACE, tbl=_TABLE, idx=_INDEX_NAME)

    def _server_pods(self) -> list[str]:
        """Return the names of the running cass-operator-managed Cassandra server pods.

        The cluster is deployed by the K8ssandra/cass-operator (see _cassandra_cluster_manifest), which
        labels server pods with ``cassandra.datastax.com/cluster=<cluster_name>``. The artifact here
        (the base table's snapshot manifest.json vs its physical sstables) is DATA-derived: with the
        deployed size:3 / RF=1 topology the inserted rows scatter by token, so the base sstable (md-4)
        may live on any single node. We therefore drive insert/flush/compact/snapshot on EVERY server
        pod and inspect EVERY pod's snapshot manifest, rather than picking one — otherwise we might
        snapshot a node that holds no base data and observe nothing.
        """
        cluster = self.app.cluster_name
        selector = f"cassandra.datastax.com/cluster={cluster}"
        out = subprocess.run(
            f"kubectl get pods -n {self.namespace} -l {selector} "
            f"-o jsonpath='{{range .items[*]}}{{.metadata.name}} {{end}}'",
            shell=True, capture_output=True, text=True,
        ).stdout.strip().strip("'")
        return [p for p in out.split() if p]

    def _exec(self, pod: str, inner: str, timeout: int = 120) -> subprocess.CompletedProcess:
        """Run ``inner`` inside the ``cassandra`` container of ``pod`` via ``bash -lc``."""
        cmd = (
            f"kubectl exec -n {self.namespace} {pod} -c cassandra -- "
            f"bash -lc {subprocess.list2cmdline([inner])}"
        )
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)

    @mark_fault_injected
    def inject_fault(self):
        """Drive the CASSANDRA-10968 snapshot-manifest reproduction on the buggy 3.11.6 server pod(s).

        Steps (all on the server pod(s)):
          1. Ensure the buggy image is active (no-op when prebuilt_from_stock pre-deployed it).
          2. Create keyspace + table + a secondary index on `city` (via cqlsh on one pod).
          3. On EVERY server pod: 3x (INSERT 2 rows; nodetool flush) so the base table and the index
             each accumulate md-1/md-2/md-3.
          4. On EVERY server pod: nodetool compact (BASE table only) -> base collapses to md-4 while the
             index keeps md-1/2/3 (unambiguous generation mismatch).
          5. On EVERY server pod: nodetool snapshot -t snap2 — writes the per-table manifest.json.
          6. On EVERY server pod: read the base table's snapshots/snap2/manifest.json and compare to the
             physical base *-Data.db files; assert the buggy signature — the manifest lists the index's
             generations (md-1/2/3) and OMITS the base sstable (md-4) that physically exists.
        """
        # 1. Make sure the buggy binary is the one running (lifecycle parity with the base class).
        if getattr(self, "_predeployed_buggy", False):
            logger.info("[AutoCassandra10968] Buggy image already deployed at cluster start — skipping swap")
        else:
            logger.info(f"[AutoCassandra10968] Injecting fault: swapping cluster to {self._custom_image}")
            self.app.inject_buggy_image(self._custom_image)
            logger.info("[AutoCassandra10968] Buggy image active")

        self.setup_preconditions()

        pods = self._server_pods()
        if not pods:
            logger.warning("[AutoCassandra10968] No Cassandra server pod found — cannot run reproducer")
            return
        logger.info(f"[AutoCassandra10968] Using server pods {pods}")

        # 2. Schema + secondary index (run once via cqlsh on the first pod — schema propagates
        #    cluster-wide; the row data lands on whichever node owns each token).
        logger.info("[AutoCassandra10968] Creating keyspace/table + secondary index")
        self._exec(pods[0], f"cqlsh -e {subprocess.list2cmdline([self._SETUP_CQL])}")

        # 3. 3x (insert a fresh pair of rows; flush) so base + index each get md-1/md-2/md-3. The
        #    inserts are cluster-wide (run once per cycle); the flush is per-pod (local sstable write).
        for cycle in range(1, 4):
            id_a, id_b = 2 * cycle - 1, 2 * cycle
            insert_cql = (
                f"INSERT INTO {_KEYSPACE}.{_TABLE} (id, email, city) "
                f"VALUES ({id_a}, 'user{id_a}@example.com', 'city{id_a}'); "
                f"INSERT INTO {_KEYSPACE}.{_TABLE} (id, email, city) "
                f"VALUES ({id_b}, 'user{id_b}@example.com', 'city{id_b}');"
            )
            logger.info(f"[AutoCassandra10968] Insert+flush cycle {cycle}/3 (ids {id_a},{id_b})")
            self._exec(pods[0], f"cqlsh -e {subprocess.list2cmdline([insert_cql])}")
            for pod in pods:
                self._exec(pod, f"nodetool flush {_KEYSPACE} {_TABLE}")

        table_glob = f"/var/lib/cassandra/data/{_KEYSPACE}/{_TABLE}-*"
        reproduced_on = []

        for pod in pods:
            # 4. Compact ONLY the base table -> single sstable md-4; index keeps md-1/2/3.
            logger.info(f"[AutoCassandra10968] [{pod}] nodetool compact {_KEYSPACE} {_TABLE} (base only)")
            self._exec(pod, f"nodetool compact {_KEYSPACE} {_TABLE}")

            # 5. Snapshot the keyspace — this writes the per-table manifest.json.
            logger.info(f"[AutoCassandra10968] [{pod}] nodetool snapshot -t {_SNAPSHOT_TAG} {_KEYSPACE}")
            self._exec(pod, f"nodetool snapshot -t {_SNAPSHOT_TAG} {_KEYSPACE}")

            # 6. Read the base table's snapshot manifest.json and compare to the physical base sstables.
            manifest_glob = f"{table_glob}/snapshots/{_SNAPSHOT_TAG}/manifest.json"
            manifest_path = self._exec(
                pod, f"ls -1 {manifest_glob} 2>/dev/null | head -n1"
            ).stdout.strip()
            if not manifest_path:
                logger.warning(
                    f"[AutoCassandra10968] [{pod}] No base-table snapshot manifest.json under {manifest_glob} "
                    "(node likely holds no base data) — skipping"
                )
                continue

            manifest_raw = self._exec(pod, f"cat {manifest_path}").stdout.strip()
            # Physical *-Data.db files directly under the base snapshot dir (NOT the index subdir).
            snap_dir = f"{table_glob}/snapshots/{_SNAPSHOT_TAG}"
            phys_base = self._exec(
                pod, f"find {snap_dir} -maxdepth 1 -name '*-Data.db' -printf '%f\\n' 2>/dev/null"
            ).stdout.strip()
            logger.info(
                f"[AutoCassandra10968] [{pod}] base manifest.json: {manifest_raw or '(empty)'}"
            )
            logger.info(
                f"[AutoCassandra10968] [{pod}] physical base *-Data.db: {phys_base or '(none)'}"
            )

            # Parse the manifest's file list and check it against the physical base sstable(s).
            try:
                listed = set(json.loads(manifest_raw).get("files", [])) if manifest_raw else set()
            except (ValueError, AttributeError):
                listed = set()
            physical = {f for f in phys_base.splitlines() if f.strip()}

            # Buggy signature: the manifest omits a physically-present base sstable, OR lists a bare
            # (no-path-prefix) file that is not physically present at the base path (the index's
            # generations leaking into the base manifest).
            missing_base = physical - listed
            phantom = {f for f in listed if "/" not in f} - physical
            if missing_base or phantom:
                reproduced_on.append(pod)
                logger.info(
                    f"[AutoCassandra10968] [{pod}] REPRODUCED — base manifest.json is wrong relative to "
                    f"the physical snapshot contents: omits base sstable(s) {sorted(missing_base) or '[]'} "
                    f"and/or lists phantom bare file(s) {sorted(phantom) or '[]'} that are not present at "
                    f"the base path (these are the index's sstable generations). manifest={manifest_raw!r}"
                )
            else:
                logger.info(
                    f"[AutoCassandra10968] [{pod}] base manifest.json matches the physical base sstables "
                    f"({sorted(physical)}) — bug not observed on this pod (likely the fixed build)."
                )

        if reproduced_on:
            logger.info(
                f"[AutoCassandra10968] Reproduced on pod(s) {reproduced_on}: the base table's snapshot "
                f"manifest.json omits the compacted base sstable and lists the index's bare generations."
            )
        else:
            logger.warning(
                "[AutoCassandra10968] Did not observe an incorrect base manifest.json on any server pod "
                "— the bug may not be present on this build (or no node held base data)."
            )
