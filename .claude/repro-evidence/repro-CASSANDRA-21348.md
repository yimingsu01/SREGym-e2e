# CASSANDRA-21348 — system_views.settings ClassCastException (Part 3 reproduction)
- Buggy version: cassandra:5.0.8 (config-gated; fix is a 5.0.8+config behavior, no version bump — use 5.0.8, source_git_ref cassandra-5.0.8)
- Shape: CONFIG-GATED (enable a startup_checks block in cassandra.yaml), then a pure-CQL SELECT.
- Root cause file: src/java/org/apache/cassandra/db/virtual/SettingsTable.java (the 5.0 system_views.settings virtual table cannot render a non-String setting value; the enum-keyed startup_checks map value is cast to String).

## Reproducer (buggy)
Enable a startup check so the `startup_checks` setting becomes an enum-keyed map, by appending to cassandra.yaml before start:
```
startup_checks:
  check_data_resurrection:
    enabled: true
```
Then:
```sql
SELECT * FROM system_views.settings;
```

## VERBATIM BUGGY SIGNATURE (5.0.8 + config)
ClassCastException: org.apache.cassandra.service.StartupChecks$StartupCheckType cannot be cast to java.lang.String

## Control
Stock 5.0.8 with NO config: the same SELECT returns rows cleanly (isolates the fault to the enum-keyed setting, not the table).
