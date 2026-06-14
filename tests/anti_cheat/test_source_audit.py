"""Unit tests for the source-edit audit trail (anti-cheat).

Exercises manifest snapshotting, diffing, suspicious-change flagging, and the
end-to-end record_rebuild against the captured baseline — all in a tmp tree with
an isolated baseline (so the developer's real ~/.sregym is never touched).
"""

import json

import pytest

from sregym.service import source_audit


@pytest.fixture
def export_tree(tmp_path, monkeypatch):
    """A fake source export with the audit dir redirected into tmp_path."""
    audit_dir = tmp_path / "audit"
    monkeypatch.setattr(source_audit, "_AUDIT_DIR", audit_dir)
    monkeypatch.setattr(source_audit, "_REBUILD_LOG", audit_dir / "rebuilds.jsonl")
    src = tmp_path / "src-export"
    (src / "src" / "java").mkdir(parents=True)
    (src / "src" / "java" / "Bug.java").write_text("class Bug { int x() { return 1; } }")
    (src / "build.xml").write_text("<project/>")
    return src


def test_snapshot_and_diff_detect_modification(export_tree):
    base = source_audit.snapshot_manifest(export_tree)
    assert "src/java/Bug.java" in base
    assert "build.xml" in base

    (export_tree / "src" / "java" / "Bug.java").write_text("class Bug { int x() { return 2; } }")
    current = source_audit.snapshot_manifest(export_tree)
    changes = source_audit.diff_manifests(base, current)
    assert changes["modified"] == ["src/java/Bug.java"]
    assert changes["added"] == [] and changes["removed"] == []


def test_diff_detects_add_and_remove(export_tree):
    base = source_audit.snapshot_manifest(export_tree)
    (export_tree / "src" / "java" / "New.java").write_text("class New {}")
    (export_tree / "build.xml").unlink()
    changes = source_audit.diff_manifests(base, source_audit.snapshot_manifest(export_tree))
    assert changes["added"] == ["src/java/New.java"]
    assert changes["removed"] == ["build.xml"]


def test_analyze_flags_no_source_change():
    flags = source_audit.analyze_changes({"added": [], "removed": [], "modified": []})
    assert "no-source-change" in flags


def test_analyze_flags_non_source_edit():
    flags = source_audit.analyze_changes({"added": [], "removed": [], "modified": ["README.md"]})
    assert "no-recognized-source-dir-touched" in flags


def test_analyze_clean_for_real_source_edit():
    flags = source_audit.analyze_changes({"added": [], "removed": [], "modified": ["src/java/Bug.java"]})
    assert flags == []


def test_record_rebuild_flags_unchanged_source(export_tree):
    # Baseline captured, then a rebuild with NO edits → flagged "no-source-change".
    source_audit.capture_baseline(export_tree)
    record = source_audit.record_rebuild("auto_cassandra_1", export_tree, image_tag="sregym/db-build:abc")
    assert record is not None
    assert record["problem_id"] == "auto_cassandra_1"
    assert record["flags"] == ["no-source-change"]

    # The record was appended to the isolated rebuild log as one JSON line.
    log_lines = source_audit._REBUILD_LOG.read_text().strip().splitlines()
    assert len(log_lines) == 1
    assert json.loads(log_lines[0])["problem_id"] == "auto_cassandra_1"


def test_record_rebuild_reports_real_edit(export_tree):
    source_audit.capture_baseline(export_tree)
    (export_tree / "src" / "java" / "Bug.java").write_text("class Bug { int x() { return 42; } }")
    record = source_audit.record_rebuild("auto_cassandra_1", export_tree)
    assert record["modified"] == ["src/java/Bug.java"]
    assert record["flags"] == []  # plausible source-level fix


def test_record_rebuild_without_baseline_is_marked(export_tree):
    # No capture_baseline() call → recorded with a "no-baseline" flag, never crashes.
    record = source_audit.record_rebuild("auto_cassandra_1", export_tree)
    assert record["flags"] == ["no-baseline"]
