"""Auto-generated from https://github.com/cockroachdb/cockroach/issues/166091

Title: gc: potential logical bug in MVCC GC
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCockroachdb166091(GenericCustomBuildProblem):
    db_name   = "cockroachdb"
    issue_url = "https://github.com/cockroachdb/cockroach/issues/166091"
    root_cause_description = (
        "gc: potential logical bug in MVCC GC. **Describe the problem** This issue was discovered in a KVNemesis test with MVCC GC operations enabled. The core issue is an assertion failure during an MVCC GC run: ``` I260316 23:35:58.386265 25881 15@kv/kvserver/mvcc_gc_queue.go:605 ⋮ [T1,Vsystem,n2,s2,r113/4:/Table/100/‹\"›{‹52e982›…-‹c0e009›…}] 5824 attempt to delete range tombstone ‹\"/Table/100/\\\"{52e982f2b9e020c3\\\"-7bece2728321e648\\\"}/1773704141.867478046,2\"› hiding key at ‹/Table/100/\"65608df5366d033b\"/1773704135.146885382,15› ``` **To Reproduce** After #165450 is merged, run the following test with many iterations (~100). This particular failure was caught in 3/100 runs. ``` func TestKVNemesisMVCCGCRepro(t *testing.T) { defer leaktest.AfterTest(t)() defer log.Scope(t).Close(t) cfg := defaultTestConfiguration(5) cfg.numSte"
    )
    reproducer = 'I260316 23:35:58.386265 25881 15@kv/kvserver/mvcc_gc_queue.go:605 ⋮ [T1,Vsystem,n2,s2,r113/4:/Table/100/‹"›{‹52e982›…-‹c0e009›…}] 5824  attempt to delete range tombstone ‹"/Table/100/\\"{52e982f2b9e020c3\\"-7bece2728321e648\\"}/1773704141.867478046,2"› hiding key at ‹/Table/100/"65608df5366d033b"/1773704135.146885382,15›'
    continuous_reproducer = True
