# Evidence Log — CASSANDRA-17918

## Bug summary (from Jira ground truth)
- **Key**: CASSANDRA-17918
- **Summary**: "DESCRIBE output does not quote column names using reserved keywords"
- **Component**: Legacy/CQL
- **fixVersions**: 4.0.10, 4.1.2, 5.0-alpha1, 5.0
- **Buggy version under test**: cassandra:4.1.1
- **Control (fixed) version**: cassandra:4.1.2  (= buggy patch + 1; 4.1.2 <= 4.1.11 ceiling -> valid A/B)

### Mechanism (from description)
`DESCRIBE TYPE` (and per the body also MV/UDF/UDA) emits UDT field names that are reserved keywords
WITHOUT quoting. Impact stated in Jira: "the schema described cannot be imported due to the usage of
reserved keywords as column names." Reproducer in the body is a DescribeStatementTest unit test that
creates a UDT with fields `"token"` and `"desc"` and asserts the DESCRIBE output quotes them.

### Classifier hint verification
- Hint topology=1node -> CORRECT. DESCRIBE is a coordinator-local CQL operation; single node suffices.
- Hint trigger (CREATE TYPE with reserved-keyword fields + DESCRIBE TYPE -> unquoted output that fails
  on re-import) -> CORRECT, matches body verbatim.
- tag_correction: none.

## Topology
Two single-node Cassandra pods in namespace `repro-17918`, same kind cluster (context kind-kind):
- `cass-buggy`  image cassandra:4.1.1  (release_version confirmed 4.1.1)
- `cass-ctrl`   image cassandra:4.1.2  (release_version confirmed 4.1.2)
Keyspace: `repro17918` (SimpleStrategy RF=1), UDT `repro17918.t`.

## Reproducer commands (identical on both pods)
```
CREATE KEYSPACE IF NOT EXISTS repro17918 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TYPE repro17918.t ("token" text, "desc" text);
DESCRIBE TYPE repro17918.t;
```

## RAW OUTPUT — DESCRIBE TYPE

### BUGGY cassandra:4.1.1  (fields emitted UNQUOTED — the bug)
```
CREATE TYPE repro17918.t (
    token text,
    desc text
);
```

### CONTROL cassandra:4.1.2  (fields correctly QUOTED — fixed)
```
CREATE TYPE repro17918.t (
    "token" text,
    "desc" text
);
```

## RAW OUTPUT — Round-trip re-import (proves the stated impact: "cannot be imported")
Each version's own DESCRIBE output is fed back as a `CREATE TYPE` (as type `t2`).

### BUGGY 4.1.1 — re-import of unquoted output FAILS
```
$ cqlsh -e "CREATE TYPE repro17918.t2 (
    token text,
    desc text
);"
<stdin>:1:SyntaxException: line 2:4 no viable alternative at input 'token' (CREATE TYPE repro17918.t2 (    [token]...)
command terminated with exit code 2
```

### CONTROL 4.1.2 — re-import of quoted output SUCCEEDS
```
$ cqlsh -e "CREATE TYPE repro17918.t2 (
    \"token\" text,
    \"desc\" text
);"
exit code: 0
# DESCRIBE TYPE repro17918.t2 then shows the type created with "token"/"desc" quoted.
```

## Verbatim buggy signature
```
    token text,
```
(unquoted reserved keyword in DESCRIBE TYPE output on 4.1.1; control 4.1.2 emits `    "token" text,`)

Secondary verbatim signature (impact, re-import of buggy output on 4.1.1):
```
<stdin>:1:SyntaxException: line 2:4 no viable alternative at input 'token' (CREATE TYPE repro17918.t2 (    [token]...)
```

## Disposition: reproduced
Client-visible wrong output: `DESCRIBE TYPE` on 4.1.1 emits reserved-keyword UDT field names without
quotes. Direct A/B control on 4.1.2 emits them quoted. The round-trip proves the Jira's stated impact:
the buggy DESCRIBE output is not re-importable (SyntaxException), while the fixed output round-trips
cleanly. Both the wrong-output signature and the re-import failure are reproduced verbatim.

## Teardown
`kubectl delete ns repro-17918 --wait=false` (only namespace created by this session).
