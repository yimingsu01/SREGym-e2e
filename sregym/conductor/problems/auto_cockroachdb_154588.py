"""Auto-generated from https://github.com/cockroachdb/cockroach/issues/154588

Title: sql/jsonpath: comparison expression with non-existent path should return `false`
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCockroachdb154588(GenericCustomBuildProblem):
    db_name   = "cockroachdb"
    issue_url = "https://github.com/cockroachdb/cockroach/issues/154588"
    root_cause_description = (
        "sql/jsonpath: comparison expression with non-existent path should return `false`. CockroachDB and PG are inconsistent in the following example: PG: ``` > CREATE TABLE t1 (a int, b jsonb); > INSERT INTO t1 VALUES (1, '{\"a\": 1}'); > SELECT a, jsonb_path_query(b, '$.l.b == 123') FROM t1 ORDER BY a; a | jsonb_path_query ---+------------------ 1 | false ``` CRDB: ``` > CREATE TABLE t1 (a int, b jsonb); > INSERT INTO t1 VALUES (1, '{\"a\": 1}'); > SELECT a, jsonb_path_query(b, '$.l.b == 123') FROM t1 ORDER BY a; a | jsonb_path_query ----+------------------- 1 | null (1 row) ``` For `SELECT a, jsonb_path_query(b, '$.l.b == 123') FROM t1 ORDER BY a;`, PG returns `false`, while CockroachDB returns `null` Jira issue: CRDB-55009"
    )
    reproducer = 'CREATE TABLE t1 (a int, b jsonb);\nINSERT INTO t1 VALUES (1, \'{"a": 1}\');\nSELECT a, jsonb_path_query(b, \'$.l.b == 123\') FROM t1 ORDER BY a;'
    continuous_reproducer = True
    expected_output = '1\tnull'
