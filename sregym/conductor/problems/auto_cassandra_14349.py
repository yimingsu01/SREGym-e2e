"""CASSANDRA-14349: Untracked CDC commit-log segment files are not deleted after replay.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-14349
Buggy: cassandra 3.11.9 (leak still present through the cassandra-3.11 branch HEAD; see note below).
"Fixed": fixVersions list 3.11.10 / 4.0-rc2, but the released 3.11.10 image does NOT contain the fix
(CHANGES.txt dated 2021-01-29 lists no 14349; the Jira resolved 2021-07-27, months later). No valid
fixed 3.11.x image exists, so there is no clean A/B control — discrimination rests on the CDC-gated
within-version result below plus source inspection of handleReplayedSegment.

STUB: config-gated CDC + kill-restart-loop resource leak — NOT encodable as a single-cluster CQL
reproducer, so this Problem is a clearly-marked stub (diagnosis-only). Why it cannot be flattened into
the framework's `reproducer` path:
  1. The bug is gated on `cdc_enabled: true` in cassandra.yaml — a server config block, not anything a
     CQL string sent through cqlsh can set.
  2. The trigger is `kill -9` of the Cassandra process followed by an IN-PLACE restart that REPLAYS the
     dirty commit log, repeated many times, with the data dir (specifically `cdc_raw/`) PERSISTING
     across each restart. The reproduction explicitly avoids pod recreation because recreating the pod
     wipes `cdc_raw/` and masks the leak. The Cassandra reproducer helper
     (`_cassandra_run_reproducer` / `_cassandra_reproducer_workload` in db_build_spec.py) only pipes CQL
     into `cqlsh`; it cannot kill/restart the server or preserve `cdc_raw/`.
  3. The buggy signature is FILESYSTEM STATE (orphaned segments accumulating in `cdc_raw/`), not a query
     result or an exception. The continuous mitigation oracle's probe is a cqlsh grep / exit-code check,
     which structurally cannot observe `cdc_raw/` accumulation — so no working mitigation oracle exists
     for this bug through the framework, and a flattened CQL reproducer would silently NOT reproduce it.
The full multi-step reproduction procedure is recorded verbatim in `reproducer` below.

Reproduction summary (single node, cassandra 3.11.9):
  1. Enable CDC (`cdc_enabled: true` in cassandra.yaml).
  2. Produce NO CDC traffic: write only to a non-CDC table so the commit log has content but no CDC data.
  3. `kill -9` the Cassandra process (NOT `nodetool drain`) so dirty commit-log segments must be REPLAYED
     on the next start.
  4. Restart in place repeatedly. On each replay the buggy
     CommitLogSegmentManagerCDC.handleReplayedSegment(File) unconditionally relocates the replayed
     segment into `cdc_raw/` (renameWithConfirm + cdcSizeTracker.addFlushedSize) with no deletion and no
     "is there any CDC data?" check, so replayed segments pile up in `cdc_raw/` forever while the live
     `commitlog/` correctly rotates/deletes them.

CDC-gated discriminator (same 3.11.9 binary, replay fires every cycle):
  - cdc_enabled: true  -> cdc_raw segment count 0 -> 2 -> 4 -> 6 across kill-restart cycles (LEAK)
  - cdc_enabled: false -> 0 -> 0 -> 0 (no leak)

Verbatim buggy signature (from the reproduction evidence log):
    CommitLog-6-1781241743009.log  linkcount=1  ORPHAN(absent-from-commitlog)
  plus the monotonic CDC_LOG_COUNT progression 0 -> 2 -> 4 -> 6 across kill-restart cycles on 3.11.9
  (every cdc_raw entry absent from the live commitlog/, each the sole remaining copy with linkcount=1).
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra14349(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "3.11.9"
    source_git_ref = "cassandra-3.11.9"
    # The bug already ships in the released 3.11.9 image, so deploy the stock image
    # rather than running a ~30-min `ant jar` source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/db/commitlog/CommitLogSegmentManagerCDC.java"
    root_cause_description = (
        "When CDC is enabled (cdc_enabled: true), commit-log segments that are REPLAYED on startup are "
        "leaked into the cdc_raw/ directory and never deleted. CommitLogSegmentManagerCDC."
        "handleReplayedSegment(File) unconditionally relocates each replayed segment into cdc_raw/ via "
        "renameWithConfirm and bumps cdcSizeTracker.addFlushedSize, with no deletion and no check for "
        "whether the segment actually contains CDC data. So after every kill -9 + restart (which forces "
        "commit-log replay), the replayed segments accumulate in cdc_raw/ as untracked orphans, while the "
        "live commitlog/ directory correctly rotates and deletes them."
    )

    # STUB: prose steps, NOT a runnable CQL block — see module docstring for why this bug cannot be
    # encoded as a single-cluster CQL reproducer. Recorded verbatim for the diagnosis oracle / future
    # multi-node encoding.
    reproducer = """
STUB: config-gated CDC + kill-restart-loop resource leak — not encodable as a single CQL reproducer.
Full reproduction procedure (single node, cassandra 3.11.9, data dir MUST persist across restarts):

  1. Start Cassandra 3.11.9 with `cdc_enabled: true` in cassandra.yaml.
  2. Create a NON-CDC keyspace/table and write rows to it (produce commit-log content but NO CDC traffic):
       CREATE KEYSPACE IF NOT EXISTS repro14349
         WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};
       USE repro14349;
       CREATE TABLE IF NOT EXISTS t (id int PRIMARY KEY, v text);
       -- bulk-insert enough rows to fill commit-log segments, then:
       INSERT INTO repro14349.t (id, v) VALUES (1, 'x');
       -- (repeat with many ids so the live commit log holds dirty, unflushed segments)
  3. Confirm cdc_raw/ is empty, then `kill -9` the Cassandra process (do NOT `nodetool drain` — the
     commit log must stay dirty so it is REPLAYED on the next start). Do NOT recreate the pod: pod
     recreation wipes cdc_raw/ and masks the leak. Restart Cassandra in place (e.g. setsid +
     docker-entrypoint via kubectl exec) so the same on-disk data dir is reused.
  4. On restart Cassandra logs "Replaying .../commitlog/CommitLog-*.log" then "Log replay complete".
  5. Repeat step 3 several times. Observe in /var/lib/cassandra (or the configured cdc_raw_directory):
       - commitlog/  holds only the CURRENT segments (replayed originals correctly deleted).
       - cdc_raw/     accumulates the replayed segments: count grows 0 -> 2 -> 4 -> 6 ...
       - each cdc_raw/ entry is absent from commitlog/ and has linkcount=1 (the only remaining copy):
           CommitLog-6-1781241743009.log  linkcount=1  ORPHAN(absent-from-commitlog)
  6. Control (same binary, cdc_enabled: false): the leak does NOT occur (cdc_raw stays 0 across all
     restarts even though replay fires each cycle), confirming the leak is CDC-gated.
"""

    # Diagnosis-only stub: the leak is filesystem state (cdc_raw/ accumulation), not a CQL result or an
    # exception, so there is NO working mitigation oracle through the CQL-only reproducer/probe path.
    # continuous_reproducer MUST stay False — otherwise the ReproducerPodMitigationOracle would pipe the
    # prose `reproducer` string above into cqlsh, which errors every loop and produces a fake-green
    # NotReady signal forever. (See generic_custom_build.py __init__ and the skill's oracle semantics.)
    continuous_reproducer = False
    # expected_output intentionally unset: this is a resource-leak bug, not a wrong-result bug.
    # crash_on_startup intentionally False: the leak grows files but startup succeeds normally.
