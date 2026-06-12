# CASSANDRA-21057 — disk-usage guardrail cannot be disabled (Part 3 reproduction)
- Buggy version: cassandra:4.1.10  -> fixed 4.1.11
- Shape: NODETOOL-SEQUENCE (setguardrailsconfig) + gossip-state observation, single node.
- Root cause file: src/java/org/apache/cassandra/service/disk/usage/DiskUsageMonitor.java (the monitor's `if (!enabled) return;` short-circuits and never re-evaluates, so DISK_USAGE stays FULL via gossip after disabling).

## Reproducer (buggy)
```
nodetool setguardrailsconfig data_disk_usage_max_disk_size 1MiB
nodetool setguardrailsconfig data_disk_usage_percentage_threshold 2 1   # [fail, warn]
# wait one 30s monitor tick -> gossip DISK_USAGE = FULL
nodetool setguardrailsconfig data_disk_usage_percentage_threshold null null   # disable
```

## VERBATIM BUGGY SIGNATURE (4.1.10)
gossip DISK_USAGE stays FULL at 30s and 60s after disabling (node never stops advertising FULL).

## Control
Fixed 4.1.11: the same disable transitions DISK_USAGE to NOT_AVAILABLE within one tick (the fix's onDiskUsageGuardrailDisabled).
