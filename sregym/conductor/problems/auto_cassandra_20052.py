"""CASSANDRA-20052: Size of CQL messages is not limited in V5 protocol logic.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-20052

Buggy: cassandra-5.0.2  ->  Fixed: cassandra-5.0.3 (also 4.1.8, 6.0).

STUB: custom binary V5 native-protocol attack (oversized AUTH_RESPONSE) plus a
PasswordAuthenticator + pinned-heap config gate — NOT expressible as a single
``reproducer`` CQL string fed through the generic cqlsh path, so this Problem is
diagnosis-only and the continuous mitigation oracle is intentionally disabled.
See the reproduction steps in the ``reproducer`` field below.

Reproduction summary (single Cassandra node, PasswordAuthenticator, -Xmx512M):
  1. Stand up a Cassandra 5.0.2 node with ``authenticator: PasswordAuthenticator``
     (so AUTH_RESPONSE is actually exchanged) and a small heap (MAX_HEAP_SIZE=512M)
     to make the OOM cheap and deterministic.
  2. From a separate client, using the bundled DataStax python driver 3.29.0
     (which supports ProtocolVersion.V5), connect over native protocol V5 with
     username='cassandra' and password='-'*600_000_000 (~572 MiB). Forcing
     protocol_version=V5 routes the message through the buggy V5 framing path
     (which has NO size limit), not the v4 path bounded by
     native_transport_max_frame_size. This emits a 600000015-byte AUTH_RESPONSE
     before authentication completes.
  3. The 5.0.2 server tries to buffer/decode the whole frame and exhausts the
     512M heap -> forced OutOfMemoryError -> the JVM's
     ``-XX:OnOutOfMemoryError="kill -9 %p"`` handler kills the Cassandra process
     (PID 1) = pre-auth DoS. (On 5.0.3 the same oversized AUTH_RESPONSE is
     rejected at the framing layer and the server stays responsive.)

Why this is NOT encoded as a runnable GenericCustomBuildProblem reproducer:
  - The trigger is a binary V5-protocol attack (a 572 MiB AUTH_RESPONSE sent via
    the DataStax python driver with protocol_version=V5), which cannot be
    represented as a cqlsh CQL string. The generic Cassandra reproducer
    runner (_cassandra_run_reproducer) and continuous workload
    (_cassandra_reproducer_workload) only pipe CQL into ``cqlsh {svc}``.
  - The bug additionally requires ``authenticator: PasswordAuthenticator`` (with
    the default AllowAllAuthenticator no AUTH_RESPONSE is ever exchanged and the
    V5 framing path is never reached) and a pinned small heap. Neither is
    settable through the GenericCustomBuildProblem class attributes / Helm args,
    and the generic cqlsh probe connects with no credentials, so enabling auth
    would break that probe anyway.
  Flattening this into a cqlsh reproducer would compile and register but
  silently fail to fire the bug (and the cqlsh-based mitigation probe would run
  the attack text as CQL, always error, and always report "bug present" even
  after a fix). An honest diagnosis-only stub is preferred.

Root cause: the V5 native-protocol framing path enforces no message-size limit.
The only size guard (native_transport_max_frame_size, 16MiB) applies only to
pre-V5 sessions / the initial STARTUP/OPTIONS handshake and is not checked in any
V5 logic, so an unauthenticated client's oversized AUTH_RESPONSE is buffered in
FrameDecoder.stash() and exhausts the heap. The fix (5.0.3) adds
native_transport_max_message_size and native_transport_max_auth_message_size
(default 128KiB) to reject oversized (auth) messages at the framing layer.

Verbatim buggy signature (from the buggy 5.0.2 system.log):
    ERROR [epollEventLoopGroup-5-14] JVMStabilityInspector.java:186 - Force heap space OutOfMemoryError in the presence of
    java.lang.OutOfMemoryError: Cannot reserve 131081 bytes of direct buffer memory (allocated: 536796132, limit: 536870912)
        at org.apache.cassandra.utils.memory.BufferPool.allocate(BufferPool.java:238)
        at org.apache.cassandra.net.BufferPoolAllocator.getAtLeast(BufferPoolAllocator.java:75)
        at org.apache.cassandra.net.FrameDecoder.stash(FrameDecoder.java:336)
        at org.apache.cassandra.net.FrameDecoderWith8bHeader.decode(FrameDecoderWith8bHeader.java:131)
        at org.apache.cassandra.net.FrameDecoderCrc.decode(FrameDecoderCrc.java:150)
        at org.apache.cassandra.net.FrameDecoder.channelRead(FrameDecoder.java:283)
    java.lang.OutOfMemoryError: Java heap space
    # -XX:OnOutOfMemoryError="kill -9 %p"
    #   Executing /bin/sh -c "kill -9 1"...
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra20052(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "5.0.2"
    source_git_ref = "cassandra-5.0.2"
    # 5.0.2 already ships the bug, so deploy the stock image instead of an ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/net/FrameDecoder.java"
    root_cause_description = (
        "Size of CQL messages is not limited in the V5 native-protocol framing logic. The only size "
        "guard, native_transport_max_frame_size (16MiB), applies only to pre-V5 sessions / the initial "
        "STARTUP/OPTIONS handshake and is not checked in any V5 logic. As a result an *unauthenticated* "
        "client can send a huge AUTH_RESPONSE (e.g. a ~572 MiB / 600000015-byte password) over protocol "
        "v5; the server buffers the whole frame in FrameDecoder.stash() (BufferPoolAllocator.getAtLeast), "
        "exhausts the heap, and a forced OutOfMemoryError triggers the JVM's "
        "-XX:OnOutOfMemoryError=\"kill -9 %p\" handler, killing the Cassandra process = pre-auth DoS. The "
        "fix (PR #3655, shipped in 5.0.3) adds native_transport_max_message_size and "
        "native_transport_max_auth_message_size (default 128KiB) to reject oversized (auth) messages."
    )

    # STUB reproducer — the full custom V5 binary-protocol attack steps. This is NOT
    # runnable through the generic cqlsh reproducer path (it is a binary V5
    # AUTH_RESPONSE sent via the DataStax python driver, and it requires the server
    # to run PasswordAuthenticator with a small heap). Recorded here verbatim for a
    # future single-cluster encoding; see the module docstring for why it is stubbed.
    reproducer = """
# === CASSANDRA-20052 reproduction (STUB — not a cqlsh CQL block) ===
#
# Server prerequisites (single Cassandra 5.0.2 node):
#   - cassandra.yaml: authenticator: PasswordAuthenticator
#       (so AUTH_RESPONSE is actually exchanged; with the default
#        AllowAllAuthenticator the bug path is never reached)
#   - heap pinned small: MAX_HEAP_SIZE=512M  (-Xmx512M) so the OOM is
#       cheap and deterministic
#   - the JVM runs with -XX:OnOutOfMemoryError="kill -9 %p" (Cassandra default),
#       so a heap OOM kills the server process
#
# Attack (run from a SEPARATE client with ample memory so the 572 MiB string is
# allocated client-side, not on the server). The bundled DataStax python driver
# 3.29.0 supports ProtocolVersion.V5 and is importable from the cassandra image at
# /opt/cassandra/lib/cassandra-driver-internal-only-3.29.0.zip/cassandra-driver-3.29.0 :
#
#   from cassandra.cluster import Cluster
#   from cassandra.auth import PlainTextAuthProvider
#   from cassandra import ProtocolVersion
#   import sys
#   target = sys.argv[1]
#   pw_len = 600_000_000  # ~572 MiB
#   auth = PlainTextAuthProvider(username='cassandra', password='-' * pw_len)
#   # Force protocol_version=V5 so the oversized AUTH_RESPONSE goes through the
#   # buggy V5 framing path (NO size limit), not the v4 path bounded by
#   # native_transport_max_frame_size.
#   cluster = Cluster([target], auth_provider=auth, protocol_version=ProtocolVersion.V5)
#   cluster.connect()   # emits a 600000015-byte AUTH_RESPONSE before auth completes
#
# Expected on buggy 5.0.2: the server logs a forced OutOfMemoryError from the V5
# frame decode path (FrameDecoder.stash / FrameDecoderCrc.decode), then
# -XX:OnOutOfMemoryError runs "kill -9 1" against the Cassandra process = DoS.
# On fixed 5.0.3: the oversized AUTH_RESPONSE is rejected at the framing layer
# ("type = AUTH_RESPONSE, size = 600000015") and the server stays responsive.
"""

    # Diagnosis-only: the root cause is known/judgeable, but the V5 binary attack
    # cannot be run through the cqlsh-based continuous workload, so do NOT build a
    # mitigation oracle (it would run the attack text as CQL, always error, and
    # falsely report the bug as present even after a fix).
    continuous_reproducer = False
