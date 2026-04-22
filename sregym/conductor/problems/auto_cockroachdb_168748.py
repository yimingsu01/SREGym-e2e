"""Auto-generated from https://github.com/cockroachdb/cockroach/issues/168748

Title: DSC: unwanted unique secondary index on DROP CONSTRAINT + ADD PRIMARY KEY
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCockroachdb168748(GenericCustomBuildProblem):
    db_name   = "cockroachdb"
    issue_url = "https://github.com/cockroachdb/cockroach/issues/168748"
    root_cause_description = (
        "DSC: unwanted unique secondary index on DROP CONSTRAINT + ADD PRIMARY KEY. When `ALTER TABLE ... DROP CONSTRAINT <pk>, ADD PRIMARY KEY (...)` appears in a single statement, the DSC incorrectly creates a unique secondary index to preserve the old PK's uniqueness — even though the user explicitly asked to drop that constraint. ### Affected versions Introduced by #159987 (merged to `master` 2025-12-22) and backported to `release-26.1` via #160012. - **26.1** (v26.1.0-prerelease and later) - **26.2** (v26.2.0-prerelease and later) ### The bug The DROP side of the combined statement marks the old PK's `IndexName` element as `ToAbsent`, and the ADD side detects this via `oldPrimaryIndexNameIsBeingDropped` to enter the standard `alterPrimaryKey` code path. However, `alterPrimaryKey` is also the path used by `ALTER PRIMARY KEY`, whose contract is to convert the old"
    )
    reproducer = 'CREATE TABLE t (a INT PRIMARY KEY, b INT);\nALTER TABLE t DROP CONSTRAINT t_pkey, ADD PRIMARY KEY (b);\nSHOW INDEXES FROM t;'
    continuous_reproducer = True
