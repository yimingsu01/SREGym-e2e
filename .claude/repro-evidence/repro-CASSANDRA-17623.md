# CASSANDRA-17623 — Frozen maps may be serialized unsorted, causing inability to query later

- **Disposition:** reproduced
- **Buggy version:** cassandra:4.0.4 (kind pod `cass404`)
- **A/B control:** cassandra:4.0.5 (kind pod `cass405`) — 4.0.5 is in fixVersions; 5 <= 4.0-line ceiling 20
- **Topology:** 1 node (single pod). Tag HINT topology=1node CONFIRMED.
- **Namespace:** repro-17623   **Keyspace:** ks17623
- **Components:** CQL/Semantics   **fixVersions:** 3.0.28, 3.11.14, 4.0.5, 4.1-alpha1, 4.1, 5.0-alpha1, 5.0

## Exact reproducer extracted from the Jira body
The body gives DDL + INSERT + SELECT, and crucially states the bug lives in `Maps.Value#fromSerialized`
and "manifests if a client sends an unsorted map as a **bound parameter**". A CQL *literal* is sorted by
the parser (`Maps.Literal`/`DelayedValue.bind` builds a TreeMap), so it does NOT reproduce. The
bound-parameter path (`fromSerialized`) is missing the sort. Therefore the reproducer MUST use a
**prepared statement** with a client-serialized, unsorted map.

DDL (verbatim from body):
```sql
CREATE TABLE ks17623.t (k text, c frozen<map<text, text>>, PRIMARY KEY (k, c));
```
Trigger: prepared INSERT bound with `OrderedDict([('z','second_value'), ('a','first_value')])` (unsorted),
then `SELECT k, c['a'] FROM t WHERE k='key'`.

## Client used
Python driver bundled inside the Cassandra pod (`/opt/cassandra/lib/cassandra-driver-internal-only-3.25.0.zip`),
path-set up exactly as `cqlsh.py` does. Connected to 127.0.0.1 INSIDE each pod via `kubectl exec` —
symmetric for both versions, no port-forward. Script: /tmp/repro17623.py.

## Wire bytes — proves the trigger (unsorted map) was IDENTICALLY staged on both
Both pods serialized the OrderedDict in insertion order — 'z' (hex `7a`) BEFORE 'a' (hex `61`):
```
WIRE_BYTES_HEX: 00000002 00000001 7a 0000000c 7365636f6e645f76616c7565 00000001 61 0000000b 66697273745f76616c7565
                 (count=2)  (len1) 'z'  (len12)  "second_value"          (len1)'a' (len11)  "first_value"
```
Identical on 4.0.4 and 4.0.5 => the driver does NOT sort; unsorted bytes reached BOTH servers. Any
difference in outcome is therefore the server-side bug, not the client.

## BUGGY 4.0.4 (cass404) — raw output
```
WIRE_BYTES_HEX: 00000002000000017a0000000c7365636f6e645f76616c756500000001610000000b66697273745f76616c7565
INSERT_DONE via prepared stmt with OrderedDict([('z',..),('a',..)])
SELECT_FULL: key {'z': 'second_value', 'a': 'first_value'}     <-- stored/returned UNSORTED
SELECT_PROJECTION c['a'] rows: 1
  c['a'] = None                                                <-- *** BUG: should be 'first_value' ***
  c['z'] = 'second_value'
```

### Physical on-disk proof (sstabledump after `nodetool flush ks17623 t`)
`/opt/cassandra/tools/bin/sstabledump <Data.db>`:
```
BUGGY 4.0.4:   "clustering" : [ {"z": "second_value", "a": "first_value"} ]   <-- INVALID: persisted unsorted
```
This is the INSERT-case corruption described in the body ("invalid data being persisted to disk").

## CONTROL 4.0.5 (cass405) — raw output (IDENTICAL script + identical wire bytes)
```
WIRE_BYTES_HEX: 00000002000000017a0000000c7365636f6e645f76616c756500000001610000000b66697273745f76616c7565
INSERT_DONE via prepared stmt with OrderedDict([('z',..),('a',..)])
SELECT_FULL: key {'a': 'first_value', 'z': 'second_value'}     <-- corrected to SORTED order
SELECT_PROJECTION c['a'] rows: 1
  c['a'] = 'first_value'                                       <-- WORKS CORRECTLY
  c['z'] = 'second_value'
```
```
CONTROL 4.0.5: "clustering" : [ {"a": "first_value", "z": "second_value"} ]  <-- correctly sorted on disk
```

## Verdict
The fix (`fromSerialized` now sorts) is present in 4.0.5. With identical unsorted wire bytes:
- 4.0.4: map persisted unsorted on disk AND `c['a']` projection returns `None` (the bug).
- 4.0.5: map persisted sorted AND `c['a']` returns `first_value` (correct).

**verbatim_signature:** `c['a'] = None`  (buggy 4.0.4 map projection on an unsorted-bound frozen map)

## Teardown
`kubectl delete ns repro-17623 --wait=false`
