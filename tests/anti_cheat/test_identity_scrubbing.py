"""Unit tests for identity-scrubbed build image tags.

A built database image is deployed during the graded task, so its tag must not
disclose the database name or upstream version (which would let an agent look up
the upstream fix instead of repairing the bug). These tests pin that property
plus the determinism the build cache and deploy handoff rely on.
"""

import re

from sregym.service.identity import opaque_build_tag

NAME = "cassandra"
VERSION = "5.0.6"
CONTENT = "deadbeef" * 8


def test_tag_hides_name_and_version():
    tag = opaque_build_tag(NAME, VERSION, CONTENT, "v1")
    assert NAME not in tag
    assert VERSION not in tag
    assert "patched" not in tag


def test_tag_shape():
    tag = opaque_build_tag(NAME, VERSION, CONTENT, "v1")
    assert re.fullmatch(r"sregym/db-build:[0-9a-f]{16}", tag), tag


def test_tag_is_deterministic():
    a = opaque_build_tag(NAME, VERSION, CONTENT, "v1")
    b = opaque_build_tag(NAME, VERSION, CONTENT, "v1")
    assert a == b


def test_tag_varies_with_each_input():
    base = opaque_build_tag(NAME, VERSION, CONTENT, "v1")
    assert opaque_build_tag("tidb", VERSION, CONTENT, "v1") != base
    assert opaque_build_tag(NAME, "5.0.7", CONTENT, "v1") != base
    assert opaque_build_tag(NAME, VERSION, "feedface" * 8, "v1") != base
    assert opaque_build_tag(NAME, VERSION, CONTENT, "v2") != base


def test_tag_parses_like_a_normal_reference():
    # The deploy path splits image references as repo:tag then registry/repository;
    # the scrubbed tag must decompose the same way the old "<db>-patched" tag did.
    tag = opaque_build_tag(NAME, VERSION, CONTENT, "v1")
    repo, sep, version_tag = tag.rpartition(":")
    assert sep == ":"
    assert repo == "sregym/db-build"
    registry, _, repository = repo.partition("/")
    assert registry == "sregym"
    assert repository == "db-build"
    assert len(version_tag) == 16
