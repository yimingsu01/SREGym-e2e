# CASSANDRA-15134 — SASI index files not included in snapshots

- **Buggy version:** cassandra:4.0.1 (pod `cass`, kind-worker2)
- **Fixed control:** cassandra:4.0.2 (pod `cass-fixed`, kind-worker3) — fixVersions include 4.0.2; ceiling 4.0->20 OK
- **Namespace:** repro-15134   **Keyspace:** repro15134_ks   **Table:** t   **Index:** t_name_sasi (SASI)
- **Topology:** 1 node (single pod). Matches classifier hint (1node).
- **Disposition:** REPRODUCED (file-level signature: SASI SI_*.db present in live dir, absent from snapshot).

## Primary source (Jira body)
"Newly written SASI index files are not being included in snapshots. This is because the SASI index files
are not added to the components (SSTable#components) list of newly written sstables. ... on startup
Cassandra does add the SASI index files (if found on disk) of existing sstables in their components list.
In that case sstables that existed on startup with SASI index files will have their SASI index files
included in any snapshots."
components: Feature/SASI, Local/Snapshots. fixVersions: 3.11.12, 4.0.2, 4.1-alpha1, 4.1.

### Reproducer extracted (followed body; matches hint)
In ONE live session (no node restart between flush and snapshot, since restart re-scans on-disk SASI
files into the components list and masks the bug):
1. Enable SASI (gated off by default in 4.0): sed `enable_sasi_indexes: false`->`true` in cassandra.yaml.
2. CREATE keyspace+table, CREATE CUSTOM INDEX ... 'org.apache.cassandra.index.sasi.SASIIndex'.
3. INSERT rows, `nodetool flush` (writes new sstables + builds SASI SI_*.db on disk).
4. `nodetool snapshot`.
5. Compare live sstable dir vs snapshot dir -> SASI SI_*.db missing from snapshot on buggy build.

## Config confirmation (both pods)
Pod arg ran: `sed -i 's/^enable_sasi_indexes:.*/enable_sasi_indexes: true/' /etc/cassandra/cassandra.yaml`
Buggy node config log: `enable_sasi_indexes=true` (Config.java line). Fixed node: same.
SASI LIKE query worked on buggy node (proves index live):
```
$ kubectl exec -n repro-15134 cass -- cqlsh --request-timeout=60 -e "SELECT * FROM repro15134_ks.t WHERE name LIKE 'al%';"
 id | name
----+-------
  1 | alpha
(1 rows)
```

## Workload commands (buggy node `cass`, 4.0.1)
```
CREATE KEYSPACE repro15134_ks WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE repro15134_ks.t (id int PRIMARY KEY, name text);
CREATE CUSTOM INDEX t_name_sasi ON repro15134_ks.t (name) USING 'org.apache.cassandra.index.sasi.SASIIndex';
INSERT INTO repro15134_ks.t (id,name) VALUES (1,'alpha'),(2,'beta'),(3,'gamma');   -- as 3 statements
nodetool flush repro15134_ks
nodetool snapshot -t repro15134_snap -kt repro15134_ks.t
  -> Requested creating snapshot(s) for [repro15134_ks.t] with snapshot name [repro15134_snap] {skipFlush=false}
```

## ===== BUGGY SIGNATURE (4.0.1) — VERBATIM =====
Data dir: /var/lib/cassandra/data/repro15134_ks/t-01a043b0661611f1ae9669dc20ef3eb2

LIVE sstable dir — SASI index files PRESENT:
```
nb-1-big-SI_t_name_sasi.db
nb-2-big-SI_t_name_sasi.db
```

SNAPSHOT dir (snapshots/repro15134_snap) listing — SASI index files ABSENT:
```
manifest.json
nb-1-big-CompressionInfo.db
nb-1-big-Data.db
nb-1-big-Digest.crc32
nb-1-big-Filter.db
nb-1-big-Index.db
nb-1-big-Statistics.db
nb-1-big-Summary.db
nb-1-big-TOC.txt
nb-2-big-CompressionInfo.db
nb-2-big-Data.db
nb-2-big-Digest.crc32
nb-2-big-Filter.db
nb-2-big-Index.db
nb-2-big-Statistics.db
nb-2-big-Summary.db
nb-2-big-TOC.txt
schema.cql
```
`find snapshots/repro15134_snap -name '*SI_*'` -> (empty: SASI files MISSING from snapshot)

Root-cause corroboration — LIVE nb-2-big-TOC.txt (on-disk components list) does NOT include the SI_ component:
```
Data.db
Summary.db
Digest.crc32
Filter.db
Statistics.db
Index.db
TOC.txt
CompressionInfo.db
```
Snapshot manifest.json (server-authored included files): {"files":["nb-2-big-Data.db","nb-1-big-Data.db"]}
grep -c SI_ manifest.json -> 0.

THE MOST-TELLING LINE: live dir contains `nb-2-big-SI_t_name_sasi.db` (and nb-1) but the snapshot dir
`snapshots/repro15134_snap` contains NO `*SI_*` file while it does copy Data.db/Index.db/Filter.db/etc.

## ===== A/B CONTROL (4.0.2) — fixed =====
Identical workload on pod `cass-fixed` (4.0.2). Data dir t-5a1b0570661611f1880a9b7b7bb60a83.

LIVE: nb-1-big-SI_t_name_sasi.db present.
SNAPSHOT dir listing — SASI index file NOW INCLUDED:
```
manifest.json
nb-1-big-CompressionInfo.db
nb-1-big-Data.db
nb-1-big-Digest.crc32
nb-1-big-Filter.db
nb-1-big-Index.db
nb-1-big-SI_t_name_sasi.db     <-- INCLUDED (fix)
nb-1-big-Statistics.db
nb-1-big-Summary.db
nb-1-big-TOC.txt
schema.cql
```
FIXED LIVE TOC.txt now lists the SI_ component (fix adds it to components list on build):
```
Index.db
Summary.db
Data.db
Statistics.db
CompressionInfo.db
TOC.txt
Digest.crc32
Filter.db
SI_t_name_sasi.db              <-- now in components list
```

## Conclusion
On buggy 4.0.1, a freshly-flushed sstable's SASI index file (SI_*.db) exists on disk but is excluded from
the sstable's components list (TOC.txt), so `nodetool snapshot` does not hard-link it into the snapshot —
the snapshot is missing the SASI index. On fixed 4.0.2 the same workload yields a snapshot that includes
the SI_*.db file, and the live TOC.txt lists the SI_ component. Reproduced + control confirmed.

Tag correction: none — hint (1node; create SASI, write+flush, snapshot -> SASI files missing) matched the
body and reproduced exactly. Only nuance: SASI must be explicitly enabled in 4.0 (enable_sasi_indexes), and
no restart may occur between flush and snapshot (startup rescan would mask it) — both handled.
