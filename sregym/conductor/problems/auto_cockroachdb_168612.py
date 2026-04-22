"""Auto-generated from https://github.com/cockroachdb/cockroach/issues/168612

Title: sql: ALTER FUNCTION RENAME/SET SCHEMA bypasses trigger dependency checks
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCockroachdb168612(GenericCustomBuildProblem):
    db_name   = "cockroachdb"
    issue_url = "https://github.com/cockroachdb/cockroach/issues/168612"
    root_cause_description = (
        "sql: ALTER FUNCTION RENAME/SET SCHEMA bypasses trigger dependency checks. ### Describe the problem `ALTER FUNCTION ... RENAME TO` and `ALTER FUNCTION ... SET SCHEMA` do not check whether the function is referenced in a trigger function body. Trigger bodies store fully-qualified function names (e.g. `public.udf_dep_helper()`), so renaming or moving the function breaks the trigger at DML time with an \"unknown function\" error. The root cause is that `TriggerDeps` tracks dependencies by descriptor ID, but `RENAME` and `SET SCHEMA` don't change the descriptor ID — so no dependency conflict is detected. However, the trigger body's textual reference to the function becomes stale. ### Steps to reproduce ```sql CREATE TABLE t (k INT PRIMARY KEY, v INT); CREATE FUNCTION udf_helper() RETURNS INT LANGUAGE SQL AS $$ SELECT 1 $$; CREATE FUNCTION trigger_fn() RETURNS TRI"
    )
    reproducer = 'CREATE TABLE t (k INT PRIMARY KEY, v INT);\n\nCREATE FUNCTION udf_helper() RETURNS INT LANGUAGE SQL AS $$ SELECT 1 $$;\n\nCREATE FUNCTION trigger_fn() RETURNS TRIGGER LANGUAGE PLpgSQL AS $$\nBEGIN\n  SELECT public.udf_helper();\n  RETURN NEW;\nEND\n$$;\n\nCREATE TRIGGER tr BEFORE INSERT ON t\nFOR EACH ROW EXECUTE FUNCTION trigger_fn();\n\n-- Works fine:\nINSERT INTO t VALUES (1, 1);\n\n-- BUG: This succeeds but should be blocked:\nALTER FUNCTION udf_helper() RENAME TO udf_helper_renamed;\n\n-- Now the trigger is broken:\nINSERT INTO t VALUES (2, 2);'
    continuous_reproducer = True
