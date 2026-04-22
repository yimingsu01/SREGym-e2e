"""Auto-generated from https://github.com/cockroachdb/cockroach/issues/153465

Title: opt: internal error when ordering `EXPLAIN` subquer
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCockroachdb153465(GenericCustomBuildProblem):
    db_name   = "cockroachdb"
    issue_url = "https://github.com/cockroachdb/cockroach/issues/153465"
    root_cause_description = (
        "opt: internal error when ordering `EXPLAIN` subquer. ```sql CREATE TABLE t ( k INT PRIMARY KEY, a INT, b INT ); SELECT info FROM [ EXPLAIN (OPT) SELECT k+1 FROM t WHERE k = 1 ] ORDER BY info; -- ERROR: internal error: runtime error: invalid memory address or nil pointer dereference -- SQLSTATE: XX000 -- DETAIL: stack trace: -- pkg/util/errorutil/catch.go:24: ShouldCatch() -- pkg/sql/opt/xform/optimizer.go:259: func1() -- GOROOT/src/runtime/panic.go:791: gopanic() -- GOROOT/src/runtime/panic.go:262: panicmem() -- GOROOT/src/runtime/signal_unix.go:917: sigpanic() -- pkg/sql/opt/xform/optimizer.go:840: setLowestCostTree() -- pkg/sql/opt/xform/optimizer.go:847: setLowestCostTree() -- pkg/sql/opt/xform/optimizer.go:285: Optimize() -- pkg/sql/plan_opt.go:876: buildExecMemo() -- pkg/sql/plan_opt.go:261: makeOptimizerPlan() -- pkg/sql/conn_"
    )
    reproducer = 'CREATE TABLE t (\n  k INT PRIMARY KEY,\n  a INT,\n  b INT\n);\n\nSELECT info FROM [\n  EXPLAIN (OPT) SELECT k+1 FROM t WHERE k = 1\n] ORDER BY info;'
    continuous_reproducer = True
