# CASSANDRA-18778 — Empty keystore_password no longer allowed on encryption_options

- **Buggy version:** cassandra:4.1.3 (post CASSANDRA-18124, which landed in 4.1.2 & 5.0)
- **Fixed control:** cassandra:4.1.4 (fix shipped in 4.1.4 / 5.0-alpha1 / 5.0; 4.1.4 <= 4.1 ceiling 11 -> A/B valid)
- **Components:** Local/Config
- **Topology:** 1 node (single pod). Classifier hint topology=1node, confidence=H — CONFIRMED correct.
- **Disposition:** REPRODUCED (verbatim startup-crash signature captured from our own pod logs).
- **Namespace:** repro-18778 (torn down after). Secret: cass-ks (empty-password PKCS12 keystore).

## Reproducer (extracted from Jira body)
After CASSANDRA-18124, `FileBasedSslContextFactory.validatePassword` rejects an EMPTY (not just null)
`keystore_password`. keytool cannot *create* an empty-password keystore but PKCS12 keystores generated
by other tools (e.g. openssl) can have empty passwords and must be readable. To trigger:
1. Create a PKCS12 keystore with an empty store password.
2. In `client_encryption_options` (Native transport) set `enabled: true`, point `keystore` at it, and set
   `keystore_password: ""`.
3. Start Cassandra -> daemon initialization aborts with ConfigurationException "Failed to initialize SSL",
   root cause `IllegalArgumentException: 'keystore_password' must be specified`.

This is a STARTUP CRASH — the pod never becomes Ready and cqlsh never answers. Evidence is taken from
`kubectl logs`, not from a readiness/cqlsh probe.

## Keystore generation (host) + verification it has an EMPTY password
```
$ openssl version
OpenSSL 3.6.2 7 Apr 2026
$ openssl req -x509 -newkey rsa:2048 -nodes -keyout k.pem -out c.pem -subj /CN=cass -days 365
$ openssl pkcs12 -export -in c.pem -inkey k.pem -out keystore.p12 -passout pass:
# verify it READS with an empty store password (via container keytool):
$ docker run --rm -v /tmp/repro-18778-ks:/ks cassandra:4.1.1 \
      keytool -list -keystore /ks/keystore.p12 -storetype PKCS12 -storepass ""
Keystore type: PKCS12
Your keystore contains 1 entry
1, Jun 12, 2026, PrivateKeyEntry, ...
```
Secret created: `kubectl create secret generic cass-ks -n repro-18778 --from-file=keystore.p12=keystore.p12`
mounted read-only at /etc/cass-ks.

## How the config was applied (in-place edit of the EXISTING block, proven via stdout)
Pod command sed-edits the default `client_encryption_options` block and greps the result to stdout
BEFORE launching, so the logs prove the effective config (no fragile dup-key / silently-failed sed).

Effective config captured in BOTH buggy and control pod logs:
```
=================== EFFECTIVE client_encryption_options ===================
1367:client_encryption_options:
1369-  enabled: true
1375-  keystore: /etc/cass-ks/keystore.p12
1376-  keystore_password: ""
==========================================================================
```

================================================================================
## BUGGY RUN — cassandra:4.1.3  (FAILS at startup)
================================================================================
Pod manifest: /tmp/repro-18778-buggy.yaml  | Full log: /tmp/repro-18778-buggy-full.log
Pod went Pending -> **Failed** (STATUS=Error) within ~6s. Did NOT become Ready.

VERBATIM buggy signature (copied from `kubectl logs cass -n repro-18778`):
```
Exception (org.apache.cassandra.exceptions.ConfigurationException) encountered during startup: Failed to initialize SSL
org.apache.cassandra.exceptions.ConfigurationException: Failed to initialize SSL
	at org.apache.cassandra.security.SSLFactory.validateSslContext(SSLFactory.java:405)
Caused by: java.lang.IllegalArgumentException: 'keystore_password' must be specified
	at org.apache.cassandra.security.FileBasedSslContextFactory.validatePassword(FileBasedSslContextFactory.java:133)
	at org.apache.cassandra.security.FileBasedSslContextFactory.buildKeyManagerFactory(FileBasedSslContextFactory.java:151)
	at org.apache.cassandra.security.SSLFactory.createNettySslContext(SSLFactory.java:168)
	at org.apache.cassandra.security.SSLFactory.validateSslContext(SSLFactory.java:355)
ERROR [main] 2026-06-12 03:01:23,768 CassandraDaemon.java:897 - Exception encountered during startup
```
The frames match the Jira body exactly (FileBasedSslContextFactory.validatePassword:133,
buildKeyManagerFactory:151, SSLFactory.validateSslContext:405/355).

================================================================================
## CONTROL RUN — cassandra:4.1.4  (IDENTICAL config -> starts cleanly)
================================================================================
Pod manifest: /tmp/repro-18778-control.yaml  | Full log: /tmp/repro-18778-control-full.log
Only difference vs buggy manifest: `image: cassandra:4.1.4`. Same secret, same sed edits,
same `keystore_password: ""`.

Pod reached **Running, 1/1 Ready**. Key startup lines (copied from logs):
```
INFO  [main] 2026-06-12 03:02:48,296 StorageService.java:3075 - Node /10.244.1.55:7000 state jump to NORMAL
INFO  [main] 2026-06-12 03:02:56,470 PipelineConfigurator.java:128 - Starting listening for CQL clients on /0.0.0.0:9042 (encrypted)...
INFO  [main] 2026-06-12 03:02:56,476 CassandraDaemon.java:761 - Startup complete
```
Negative check (count of the buggy signature in the control log):
```
$ kubectl logs cass -n repro-18778 | grep -c "must be specified"
0
```
The empty-password keystore is genuinely accepted: CQL is listening "(encrypted)" with the SAME
empty `keystore_password: ""`. The fix (validatePassword now only rejects null, not empty) is confirmed.

## A/B verdict
| image | empty keystore_password | result |
|-------|--------------------------|--------|
| 4.1.3 (buggy)  | "" | startup ABORTS: IllegalArgumentException 'keystore_password' must be specified |
| 4.1.4 (fixed)  | "" | starts clean: CQL listening (encrypted), Startup complete, 0 errors |

## Tag correction
None. Classifier hint (topology=1node, trigger=empty keystore_password -> ConfigurationException
'Failed to initialize SSL' at startup) matches the Jira body and the observed behavior exactly.
Refinement: the most-telling root-cause line is the `IllegalArgumentException: 'keystore_password'
must be specified` at FileBasedSslContextFactory.validatePassword:133 (more specific than the generic
"Failed to initialize SSL"). Triggered via the client_encryption_options path ("Native transport").

## Notes on image
Candidate buggy version 4.1.3 was used as-is (pulled onto all kind workers from Docker Hub). 4.1.2 is
also post-18124 and equally buggy but was not needed.
