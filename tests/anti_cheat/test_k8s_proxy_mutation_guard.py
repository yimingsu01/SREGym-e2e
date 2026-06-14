"""Unit tests for the Kubernetes API proxy anti reward-hacking guards.

These cover the pure decision logic that prevents an agent from tampering with
benchmark-internal scaffolding (the continuous reproducer the mitigation oracle
reads, and load generators) by addressing it directly by name. No cluster or
running proxy server is required — the upstream GET is injected as a callable.
"""

import json

import pytest
import yaml

from sregym.service.db_build_spec import _workload_manifest
from sregym.service.k8s_proxy import (
    HIDDEN_LABELS,
    is_namespaced_collection,
    metadata_has_hidden_label,
    mutation_forbidden,
    named_resource_get_path,
    payload_targets_hidden,
)

REPRODUCER_LABELS = {"sregym.io/internal": "true"}
NORMAL_LABELS = {"app": "mysql", "app.kubernetes.io/name": "tidb"}


# ── named_resource_get_path ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        # Single named namespaced resource → its own GET path.
        ("/api/v1/namespaces/db/pods/db-reproducer-x", "/api/v1/namespaces/db/pods/db-reproducer-x"),
        (
            "/apis/apps/v1/namespaces/db/deployments/db-reproducer",
            "/apis/apps/v1/namespaces/db/deployments/db-reproducer",
        ),
        # Subresources are stripped so the parent object is fetched.
        (
            "/apis/apps/v1/namespaces/db/deployments/db-reproducer/scale",
            "/apis/apps/v1/namespaces/db/deployments/db-reproducer",
        ),
        (
            "/apis/apps/v1/namespaces/db/deployments/db-reproducer/status",
            "/apis/apps/v1/namespaces/db/deployments/db-reproducer",
        ),
        # Query string is stripped.
        (
            "/api/v1/namespaces/db/configmaps/db-reproducer?fieldManager=kubectl",
            "/api/v1/namespaces/db/configmaps/db-reproducer",
        ),
        # Collection (no name), namespace object, and cluster-scoped → None.
        ("/api/v1/namespaces/db/pods", None),
        ("/api/v1/namespaces/db", None),
        ("/api/v1/nodes/worker-1", None),
        ("/healthz", None),
    ],
)
def test_named_resource_get_path(path, expected):
    assert named_resource_get_path(path) == expected


# ── metadata_has_hidden_label ─────────────────────────────────────────────────


def test_metadata_has_hidden_label_reproducer():
    assert metadata_has_hidden_label({"labels": REPRODUCER_LABELS}, HIDDEN_LABELS) is True


def test_metadata_has_hidden_label_loadgen():
    assert metadata_has_hidden_label({"labels": {"app": "load-generator"}}, HIDDEN_LABELS) is True


def test_metadata_has_hidden_label_normal():
    assert metadata_has_hidden_label({"labels": NORMAL_LABELS}, HIDDEN_LABELS) is False


def test_metadata_has_hidden_label_empty():
    assert metadata_has_hidden_label({}, HIDDEN_LABELS) is False
    assert metadata_has_hidden_label(None, HIDDEN_LABELS) is False


# ── mutation_forbidden ────────────────────────────────────────────────────────


def _fetch_hidden(_get_path):
    return {"labels": REPRODUCER_LABELS}


def _fetch_normal(_get_path):
    return {"labels": NORMAL_LABELS}


def _fetch_missing(_get_path):
    return None


def test_mutation_get_is_allowed():
    # Read verbs are never blocked here (list/get filtering is handled elsewhere).
    assert mutation_forbidden("GET", "/api/v1/namespaces/db/pods/x", None, HIDDEN_LABELS, _fetch_hidden) is False


def test_mutation_patch_hidden_blocked():
    assert (
        mutation_forbidden(
            "PATCH", "/apis/apps/v1/namespaces/db/deployments/db-reproducer", b"{}", HIDDEN_LABELS, _fetch_hidden
        )
        is True
    )


def test_mutation_delete_hidden_blocked():
    assert (
        mutation_forbidden(
            "DELETE", "/apis/apps/v1/namespaces/db/deployments/db-reproducer", None, HIDDEN_LABELS, _fetch_hidden
        )
        is True
    )


def test_mutation_scale_subresource_hidden_blocked():
    # Scaling the reproducer to 0 must be blocked even via the /scale subresource.
    assert (
        mutation_forbidden(
            "PUT",
            "/apis/apps/v1/namespaces/db/deployments/db-reproducer/scale",
            b'{"spec":{"replicas":0}}',
            HIDDEN_LABELS,
            _fetch_hidden,
        )
        is True
    )


def test_mutation_patch_normal_allowed():
    # Patching a real application Deployment (the legitimate fix) is allowed.
    assert (
        mutation_forbidden(
            "PATCH", "/apis/apps/v1/namespaces/app/deployments/frontend", b"{}", HIDDEN_LABELS, _fetch_normal
        )
        is False
    )


def test_mutation_collection_path_allowed():
    # A path with no resource name cannot be guarded by name (fetch not attempted).
    assert mutation_forbidden("DELETE", "/api/v1/namespaces/db/pods", None, HIDDEN_LABELS, _fetch_hidden) is False


def test_mutation_fetch_missing_fails_open():
    # If the target cannot be fetched (e.g. 404) the mutation is allowed.
    assert (
        mutation_forbidden(
            "PATCH", "/apis/apps/v1/namespaces/db/deployments/db-reproducer", b"{}", HIDDEN_LABELS, _fetch_missing
        )
        is False
    )


def test_mutation_post_hidden_label_blocked():
    body = json.dumps({"metadata": {"labels": REPRODUCER_LABELS}}).encode()
    assert mutation_forbidden("POST", "/api/v1/namespaces/db/configmaps", body, HIDDEN_LABELS, _fetch_normal) is True


def test_mutation_post_normal_allowed():
    body = json.dumps({"metadata": {"labels": NORMAL_LABELS}}).encode()
    assert mutation_forbidden("POST", "/api/v1/namespaces/db/configmaps", body, HIDDEN_LABELS, _fetch_normal) is False


def test_mutation_post_no_body_allowed():
    assert mutation_forbidden("POST", "/api/v1/namespaces/db/configmaps", None, HIDDEN_LABELS, _fetch_normal) is False


def test_mutation_post_invalid_json_allowed():
    assert (
        mutation_forbidden("POST", "/api/v1/namespaces/db/configmaps", b"not json", HIDDEN_LABELS, _fetch_normal)
        is False
    )


# ── is_namespaced_collection ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        # Namespaced collections (list) — must be filtered, including kubectl Table output.
        ("/api/v1/namespaces/db/configmaps", True),
        ("/api/v1/namespaces/db/pods?limit=500", True),
        ("/apis/apps/v1/namespaces/db/deployments", True),
        # Single named resource, subresource, namespace object → not a collection.
        ("/api/v1/namespaces/db/configmaps/hidden-cm", False),
        ("/apis/apps/v1/namespaces/db/deployments/db-reproducer/scale", False),
        ("/api/v1/namespaces/db", False),
        # Cluster-wide collections are classified by _should_filter_response, not here.
        ("/api/v1/configmaps", False),
        ("/healthz", False),
    ],
)
def test_is_namespaced_collection(path, expected):
    assert is_namespaced_collection(path) == expected


# ── payload_targets_hidden ────────────────────────────────────────────────────


def test_payload_targets_hidden_raw_object():
    # Raw object form (kubectl get -o json): labels at the top level.
    obj = {"kind": "ConfigMap", "metadata": {"labels": REPRODUCER_LABELS}}
    assert payload_targets_hidden(obj, HIDDEN_LABELS) is True


def test_payload_targets_hidden_table_get_by_name():
    # kubectl's default Table for a get-by-name nests the real object under rows[].object;
    # the top-level metadata is only the table's own and must not mask the hidden label.
    table = {
        "kind": "Table",
        "metadata": {"resourceVersion": "123"},
        "rows": [{"object": {"metadata": {"labels": REPRODUCER_LABELS}}}],
    }
    assert payload_targets_hidden(table, HIDDEN_LABELS) is True


def test_payload_targets_hidden_normal_raw():
    assert payload_targets_hidden({"metadata": {"labels": NORMAL_LABELS}}, HIDDEN_LABELS) is False


def test_payload_targets_hidden_normal_table():
    table = {"kind": "Table", "metadata": {}, "rows": [{"object": {"metadata": {"labels": NORMAL_LABELS}}}]}
    assert payload_targets_hidden(table, HIDDEN_LABELS) is False


def test_payload_targets_hidden_non_dict():
    assert payload_targets_hidden(None, HIDDEN_LABELS) is False
    assert payload_targets_hidden([], HIDDEN_LABELS) is False


# ── reproducer manifest carries the hidden marker ─────────────────────────────


def test_reproducer_manifest_is_marked_hidden():
    manifest = _workload_manifest(
        cluster_name="db",
        namespace="db",
        client_image="mysql:8.0",
        loop_cmd="echo hi",
        script_content="SELECT 1;",
        script_filename="run.sql",
        probe_script="exit 0",
    )
    docs = {d["kind"]: d for d in yaml.safe_load_all(manifest) if d}

    # ConfigMap and Deployment metadata both carry the hidden marker.
    assert docs["ConfigMap"]["metadata"]["labels"]["sregym.io/internal"] == "true"
    assert docs["Deployment"]["metadata"]["labels"]["sregym.io/internal"] == "true"

    # The pod template carries the marker too (so reproducer pods are hidden).
    pod_labels = docs["Deployment"]["spec"]["template"]["metadata"]["labels"]
    assert pod_labels["sregym.io/internal"] == "true"

    # The oracle's selector label is preserved so grading still finds the pods.
    assert pod_labels["app"] == "db-reproducer"
    assert docs["Deployment"]["spec"]["selector"]["matchLabels"]["app"] == "db-reproducer"
