"""CASSANDRA-17840: IndexOutOfBoundsException in paging-state version inference (V3 state on a V4 connection).

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-17840
Title: IndexOutOfBoundsException in Paging State Version Inference (integer overflow in PagingState).
Buggy: 4.0.5 (also confirmed buggy in 4.0.6). Fixed: 4.0.7.
  NOTE: the JIRA ``fixVersions`` field lists 4.0.6, but per the reproduction evidence log that is wrong —
  4.0.6 still reproduces identically and its source is unchanged. The first fixed 4.0.x release is 4.0.7
  (CHANGES.txt: "Fix potential IndexOutOfBoundsException in PagingState in mixed mode clusters
  (CASSANDRA-17840)").

STUB: raw native-protocol reproduction — a crafted ``paging_state`` sent over a CQL native protocol v4
QUERY — cannot be driven by SREGym's cqlsh-based reproducer / workload runner (cqlsh cannot inject an
arbitrary raw paging_state), so this is NOT encoded as a runnable reproducer. The full raw-protocol steps
from the evidence log are recorded in the ``reproducer`` string below for manual reproduction.

Reproduction summary (single node, buggy 4.0.5):
  Send a CQL native protocol v4 QUERY with the with_paging_state flag (0x08) set and a crafted
  paging_state whose first unsigned-VInt (partitionKeyLen) encodes a large positive value (0x7FFFFFFF).
  In PagingState.isModernSerialized, `int index += computeUnsignedVIntSize(partitionKeyLen) + partitionKeyLen`
  overflows int to -2147483644, passes the `index >= limit` check, then getUnsignedVInt ->
  input.get(-2147483644) throws IndexOutOfBoundsException. Because it is not an IOException it escapes
  deserialize()'s `catch (IOException)` and surfaces to the client as a SERVER_ERROR (opcode 0x00, error code
  0x00000000) rather than a clean PROTOCOL_ERROR. (Fixed 4.0.7 returns PROTOCOL_ERROR 0x0a "Invalid value for
  the paging state".)

Verbatim buggy signature (from the server over native protocol):
  java.lang.IndexOutOfBoundsException: -2147483644
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra17840(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.0.5"
    source_git_ref = "cassandra-4.0.5"
    # 4.0.5 is a released image that already ships the bug — deploy the stock image
    # instead of running a ~30-min ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/service/pager/PagingState.java"
    root_cause_description = (
        "IndexOutOfBoundsException in paging-state version inference. In "
        "PagingState.isModernSerialized, `index` is an `int` and the code does "
        "`index += computeUnsignedVIntSize(partitionKeyLen) + partitionKeyLen` (int += long) with no "
        "Math.toIntExact / overflow guard. A crafted paging_state whose first unsigned-VInt "
        "(partitionKeyLen) encodes a large positive value (e.g. 0x7FFFFFFF) overflows `index` to a negative "
        "number (-2147483644), which still passes the `index >= limit` check; getUnsignedVInt is then called "
        "with that negative index and `input.get(-2147483644)` throws IndexOutOfBoundsException. Because that "
        "is not an IOException it escapes deserialize()'s `catch (IOException)` and leaks to the client as a "
        "SERVER_ERROR instead of a clean PROTOCOL_ERROR. Fixed by reading partitionKeyLen via toIntExact() "
        "(throws on overflow) and adding an `index < 0` guard (addNonNegative) in 4.0.7."
    )

    # STUB reproducer: NOT runnable via the cqlsh-based reproducer runner. cqlsh cannot inject a raw,
    # arbitrary paging_state, so the bug was reproduced with a pure-Python stdlib raw native-protocol
    # client copied into the Cassandra pod (the client source is not preserved in the evidence log).
    # The concrete steps below are recorded for manual reproduction.
    reproducer = """
-- STUB: raw native-protocol reproduction; cqlsh CANNOT inject an arbitrary paging_state.
-- Single node, buggy cassandra:4.0.5. Steps reconstructed from the reproduction evidence log.
--
-- 1. Seed a small table (a valid paging_state can then also be obtained as a control):
--      CREATE KEYSPACE IF NOT EXISTS repro17840
--        WITH replication = {'class':'SimpleStrategy','replication_factor':1};
--      CREATE TABLE IF NOT EXISTS repro17840.t (id int PRIMARY KEY, v text);
--      INSERT INTO repro17840.t (id, v) VALUES (1,'a');  -- ... through (5,'e')  (5 rows total)
--
-- 2. Open a CQL native protocol v4 connection to 127.0.0.1:9042 (negotiates v4) and send a QUERY frame
--    with the with_paging_state flag 0x08 set and a crafted, length-prefixed paging_state.
--    cqlsh cannot do this; a raw stdlib Python client (e.g. /tmp/repro17840c.py, copied into the pod) is
--    required, then run:  kubectl exec -n <ns> <cass-pod> -- python3 /tmp/repro17840c.py
--
--    Malicious paging_state bytes (hex): f0 7f ff ff ff
--      = a 5-byte unsigned VInt decoding to partitionKeyLen = 2147483647 (0x7FFFFFFF)
--      index = position(0) + computeUnsignedVIntSize(2147483647)=5 + 2147483647 = 2147483652
--      (int)2147483652 = -2147483644  -> passes the `index >= limit` check
--      -> getUnsignedVInt -> input.get(-2147483644) -> IndexOutOfBoundsException
--
--    Valid (control) modern paging_state bytes (hex): 04 00 00 00 01 00 f0 7f ff ff fd 00
--      (resumes a page_size=2 query correctly -> RESULT kind=2).
--
-- 3. BUGGY 4.0.5 reply to the malicious frame:
--      opcode=0x00 (ERROR), error code=0x00000000 (SERVER_ERROR),
--      message: java.lang.IndexOutOfBoundsException: -2147483644
--    FIXED 4.0.7 reply:
--      opcode=0x00 (ERROR), error code=0x0000000a (PROTOCOL_ERROR),
--      message: Invalid value for the paging state
"""
    # Diagnosis-only stub: the bug is an error/crash (SERVER_ERROR, not a wrong result), and the cqlsh
    # workload pod cannot drive a raw paging_state frame, so a continuous mitigation oracle would always
    # report "fixed" (false oracle). Leave continuous_reproducer False and expected_output unset.
    continuous_reproducer = False
