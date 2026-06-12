# CASSANDRA-14477 — Reproduction Evidence Log

**Summary (Jira):** "The check of num_tokens against the length of inital_token in the yaml triggers unexpectedly"
**Buggy version:** cassandra:3.11.8
**A/B fixed control:** cassandra:3.11.19 (fix present here; buggy+1 = 3.11.9 is NOT fixed — see below)
**Components:** Local/Config   **fixVersions (Jira):** 3.0.23, 3.11.9, 4.0-beta4, 4.0
**Namespace:** repro-14477 (isolated)   **Topology:** single node (1node) — matches hint
**Disposition:** reproduced

---

## 1. Bug mechanism (from Jira body + source)

CASSANDRA-10120 added a check in `DatabaseDescriptor.applyInitialTokens()` comparing
`num_tokens` against the count of tokens in `initial_token`. CASSANDRA-14477 reports that this
comparison runs **regardless of whether `num_tokens` is present in the yaml**. When `num_tokens`
is absent it silently defaults to the `Config` default and the check fires anyway, aborting
startup with a misleading message that implies the operator set a conflicting `num_tokens`.

### Buggy code — `cassandra-3.11.8` `src/java/org/apache/cassandra/config/Config.java:81`
```java
public int num_tokens = 1;          // primitive int, default 1 — cannot tell "unset" from "set to 1"
```
### Buggy code — `cassandra-3.11.8` `DatabaseDescriptor.java:945-956`
```java
public static void applyInitialTokens() {
    if (conf.initial_token != null) {
        Collection<String> tokens = tokensFromString(conf.initial_token);
        if (tokens.size() != conf.num_tokens)            // fires even when num_tokens was never set
            throw new ConfigurationException("The number of initial tokens (by initial_token) specified is different from num_tokens value", false);
        ...
```

### Fixed code — `cassandra-3.11.19` `Config.java:81` + `DatabaseDescriptor.applyTokensConfig()`
```java
public Integer num_tokens;          // now nullable: null == "not set in yaml"
...
if (conf.num_tokens == null) {
    if (tokens.size() == 1) conf.num_tokens = 1;                          // single token + unset => infer 1, start OK
    else throw new ConfigurationException("initial_token was set but num_tokens is not!", false);  // accurate msg
}
if (tokens.size() != conf.num_tokens) { ... different from num_tokens value (with numbers) ... }
```

**Reproducer (exact):** remove `num_tokens` from cassandra.yaml so it is absent, set
`initial_token: 100,200` (two tokens). In 3.11.8, `tokens.size()=2 != num_tokens(default 1)` →
spurious abort with a misleading "different from num_tokens value" message even though the
operator never set num_tokens. (Note: `CASSANDRA_NUM_TOKENS` env is intentionally NOT set, so the
docker-entrypoint does not re-inject num_tokens.)

---

## 2. Pod spec (identical config on both images)

Pod command (both pods):
```bash
sed -i '/^num_tokens:/d' /etc/cassandra/cassandra.yaml
echo 'initial_token: 100,200' >> /etc/cassandra/cassandra.yaml
echo '--- effective token config ---'
grep -nE '^(num_tokens|initial_token)' /etc/cassandra/cassandra.yaml || echo 'num_tokens ABSENT'
exec docker-entrypoint.sh cassandra -f
```
`restartPolicy: Never`. Fixed pod pinned to `kind-worker` with `imagePullPolicy: Never`
(3.11.19 image was already loaded there; Docker Hub returned HTTP 429 on a fresh pull).

```
$ kubectl get pods -n repro-14477
NAME         READY   STATUS   ...   NODE
cass-buggy   0/1     Error          kind-worker   (3.11.8)
cass-fixed   0/1     Error          kind-worker   (3.11.19)
```
Both abort at config parse (~within 1s of Config load). The discriminator is the MESSAGE.

---

## 3. BUGGY signature — cassandra:3.11.8 (VERBATIM from `kubectl logs cass-buggy`)

Effective yaml (num_tokens absent, only initial_token present):
```
--- effective token config (buggy 3.11.8) ---
1279:initial_token: 100,200
```
Node configuration dump confirms num_tokens silently defaulted to 1 (never set by operator):
```
... initial_token=100,200; ... num_tokens=1; ...
```
**The buggy exception (THE signature):**
```
Exception (org.apache.cassandra.exceptions.ConfigurationException) encountered during startup: The number of initial tokens (by initial_token) specified is different from num_tokens value
The number of initial tokens (by initial_token) specified is different from num_tokens value
ERROR [main] 2026-06-12 04:25:55,511 CassandraDaemon.java:785 - Exception encountered during startup: The number of initial tokens (by initial_token) specified is different from num_tokens value
```
This is the spurious/unexpected trigger: the message blames a num_tokens mismatch, but the
operator never set num_tokens at all — exactly the behaviour CASSANDRA-14477 reports.

---

## 4. FIXED A/B control — cassandra:3.11.19 (VERBATIM from `kubectl logs cass-fixed`)

Identical effective yaml (num_tokens absent, initial_token=100,200):
```
--- effective token config (fixed 3.11.19) ---
initial_token: 100,200
```
**The fixed exception (corrected message):**
```
Exception (org.apache.cassandra.exceptions.ConfigurationException) encountered during startup: initial_token was set but num_tokens is not!
ERROR [main] 2026-06-12 04:26:46,183 CassandraDaemon.java:808 - Exception encountered during startup: initial_token was set but num_tokens is not!
```
The fix replaces the misleading "different from num_tokens value" with the accurate
"initial_token was set but num_tokens is not!" and makes `num_tokens` nullable so a single
`initial_token` with `num_tokens` unset now infers `num_tokens=1` and starts cleanly.

**Both versions reject THIS particular config** (2 tokens + num_tokens unset is genuinely
ambiguous), but only the buggy version emits the misleading "mismatch" diagnostic. The
operator-visible behaviour reported in the ticket (the spurious/misleading trigger) reproduces
verbatim on 3.11.8 and is corrected on 3.11.19.

---

## 5. Control note on buggy+1 (3.11.9)

The Jira lists fixVersion **3.11.9**, but the released `cassandra-3.11.9` source is BYTE-IDENTICAL
to 3.11.8 for this code path:
- `Config.java:81` -> `public int num_tokens = 1;` (unchanged)
- `DatabaseDescriptor.applyInitialTokens()` lines 945-956 -> identical `tokens.size() != conf.num_tokens`
Therefore cassandra:3.11.9 (buggy patch+1) is NOT a valid fixed control — it throws the same
misleading message as 3.11.8. The actual fix (nullable `Integer num_tokens` + `applyTokensConfig`)
is present by **3.11.19**, which is the control used above.

---

## 6. Verbatim signature (single most-telling line)

```
Exception (org.apache.cassandra.exceptions.ConfigurationException) encountered during startup: The number of initial tokens (by initial_token) specified is different from num_tokens value
```

## 7. Teardown
`kubectl delete ns repro-14477 --wait=false` (executed after writing this log).
