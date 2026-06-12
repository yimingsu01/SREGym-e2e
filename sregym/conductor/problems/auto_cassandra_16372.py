"""CASSANDRA-16372: cqlsh COPY FROM drops rows whose collection contains an empty string.

Title: Import from csv of empty strings in list fails with a ParseError ("Empty
values are not allowed,  given up without retries"), silently dropping the row.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16372
Buggy: 3.11.9   Fixed: 3.11.10 (also 3.0.24, 4.0-rc1, 4.0)

Reproduction (single cqlsh session, client-side COPY/CSV bug — no ring needed):
  1. CREATE TABLE with a list<text> column, then INSERT a row whose list has an
     empty-string element: ['But if you now try to wash your hands,', ''].
  2. COPY ... TO '/tmp/ctm.csv' exports the 1 row successfully.
  3. TRUNCATE the table, then COPY ... FROM '/tmp/ctm.csv' to re-import.
  4. The import raises a ParseError and DROPS the row; the final SELECT returns
     0 rows — silent data loss on a round-trip export/import.

On the buggy build cqlsh exits non-zero (exit code 2) on the COPY FROM ParseError;
on the fixed build the same workload re-imports the row intact and exits 0.

Verbatim buggy signature (from the reproduction evidence log; note the double
space before "given up"):
  <stdin>:8:Failed to import 1 rows: ParseError - Failed to parse ['But if you now try to wash your hands,', ''] : Empty values are not allowed,  given up without retries

STUB: client-side cqlsh bug — NOT reproducible through SREGym's standard
single-node-cql encoding, so this Problem is diagnosis-only and intentionally does
NOT arm a mitigation oracle. The fix lives in pylib/cqlshlib/copyutil.py, a cqlsh
*client* library, so reproduction depends entirely on the cqlsh BINARY that runs
COPY FROM — not on the Cassandra server. SREGym's cassandra reproducer paths
(db_build_spec.py: _cassandra_run_reproducer and _cassandra_reproducer_workload)
both run cqlsh from a hardcoded cassandra:4.1 client pod, and 4.1's cqlsh already
contains the 16372 fix (fixVersions include 4.0). So swapping in the buggy 3.11.9
server image never exercises the buggy cqlsh: the standard reproducer would compile
and register but silently exit 0 (no ParseError) on BOTH the buggy and fixed builds.
Because of that, continuous_reproducer is left False: a continuous mitigation oracle
with no expected_output (expect_unready=False) would read the always-exit-0 4.1
client pod as Ready=mitigated on both builds — a false pass.

Faithful reproduction needs a VERSION-MATCHED cqlsh, e.g. cqlsh exec'd INSIDE the
buggy 3.11.9 server pod (k8ssandra/cass-management-api:3.11.9-ubi8) using the
K8ssandra superuser credentials — see sregym/service/apps/cassandra.py run_cql for
the proven `kubectl exec -i -c cassandra ... cqlsh -u "$U" -p "$P"` pattern. That is
not currently plumbable here: GenericDBApplication exposes no credential helper and
db_build_spec.py provides no per-problem client_image override, and whether the
management-api image ships a 3.11.9 cqlsh cannot be verified statically. Encoding it
would require either a custom inject_fault() that execs the buggy server pod (and a
credential helper) or a client_image override in the cassandra DBBuildSpec.
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra16372(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "3.11.9"
    source_git_ref = "cassandra-3.11.9"
    # The fix shipped in 3.11.10, so 3.11.9 is the stock buggy release — deploy the
    # stock image instead of running a ~30-min `ant jar` source build.
    prebuilt_from_stock = True

    root_cause_file = "pylib/cqlshlib/copyutil.py"
    root_cause_description = (
        "cqlsh's COPY FROM (CSV import) rejects an empty string when it appears as an "
        "element inside a collection (e.g. list<text>). In pylib/cqlshlib/copyutil.py the "
        "value-conversion path treats an empty value as the null marker and raises "
        "ParseError 'Empty values are not allowed', so the whole row fails to import and is "
        "silently dropped. The fix distinguishes empty strings from nulls for VARCHAR-typed "
        "collection elements (checking the element type is not a VarcharType before treating "
        "an empty value as null), so empty-string list elements round-trip through "
        "COPY TO / COPY FROM intact."
    )

    # Single cqlsh session: COPY TO writes /tmp/ctm.csv and COPY FROM reads it back
    # within the same run, so the file round-trips. TRUNCATE + the fixed-UUID INSERT
    # re-arm the table each pass; CREATE ... IF NOT EXISTS keeps the loop idempotent
    # so the FIXED build stays at exit 0 across iterations (buggy stays at exit 2).
    reproducer = """
CREATE KEYSPACE IF NOT EXISTS repro_16372 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE IF NOT EXISTS repro_16372.test_1 ( uid uuid PRIMARY KEY, texts list<text> );
INSERT INTO repro_16372.test_1 (uid, texts) VALUES (833fee3f-d4f9-418b-9387-84ac2cda5cb7, ['But if you now try to wash your hands,', '']);
SELECT * FROM repro_16372.test_1;
COPY repro_16372.test_1 (uid, texts) TO '/tmp/ctm.csv';
TRUNCATE TABLE repro_16372.test_1;
COPY repro_16372.test_1 (uid, texts) FROM '/tmp/ctm.csv';
SELECT * FROM repro_16372.test_1;
"""
    # Diagnosis-only (see module docstring STUB note): continuous_reproducer is
    # intentionally False because SREGym's mitigation oracle runs cqlsh from a
    # cassandra:4.1 client whose cqlsh already has the fix, so a continuous probe
    # would read Ready=mitigated on both the buggy and fixed builds (a false pass).
    # Leaving it False gives a diagnosis LLMAsAJudgeOracle on root_cause_description
    # and NO ReproducerPodMitigationOracle — the honest oracle for this bug.
    continuous_reproducer = False
    # NOTE: this is an error/exit-code bug (ParseError), NOT a wrong-result bug, so
    # expected_output is intentionally unset (no incorrect value is persisted/returned;
    # the row is simply dropped). expected_output would only matter for a continuous
    # wrong-result probe, which this Problem does not arm.
