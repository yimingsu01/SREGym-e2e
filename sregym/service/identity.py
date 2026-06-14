"""Identity scrubbing for benchmark-built database images.

When the conductor builds the buggy (or agent-rebuilt) database image it must
tag it before loading it into the cluster.  A naive tag such as
``sregym/cassandra-patched:5.0.6-ab12cd34`` leaks two pieces of the answer key
straight into the running pod spec: the **database name** and the **exact
upstream version**.  An agent that runs ``kubectl get pod -o yaml`` could read
those off the image reference and look up the upstream fix (issue, PR, or the
next commit) instead of diagnosing and repairing the bug itself.

``opaque_build_tag`` folds the name and version into a hash so the deployed
image reference reveals neither, while staying **deterministic** in its inputs
so the local build cache and the build → load → deploy handoff (which all key
off this exact string) keep working unchanged.

Note (scope): this scrubs the image SREGym builds and deploys during the graded
task.  A database still self-reports its version over its own protocol
(``SELECT version()``, ``nodetool version``, ...), and the stock base image is
visible before fault injection.  Those are not removable by retagging; the
decisive control against looking up the upstream fix is network egress
restriction combined with the git-free, scrubbed source export.
"""

import hashlib

# Neutral repository component shared by every database build so the repository
# half of the reference does not disclose which database is under test either.
_OPAQUE_REPO = "sregym/db-build"


def opaque_build_tag(
    name: str,
    version: str,
    content_hash: str,
    dockerfile_version: str = "",
    repo: str = _OPAQUE_REPO,
) -> str:
    """Return a deterministic, identity-scrubbed local image tag.

    Args:
        name: database name (e.g. ``cassandra``) — folded into the hash, never emitted.
        version: upstream version (e.g. ``5.0.6``) — folded into the hash, never emitted.
        content_hash: hash of the source/patch contents, for cache keying.
        dockerfile_version: bumped when the build recipe changes, to bust the cache.
        repo: neutral repository component for the reference.

    The returned reference has the shape ``{repo}:{16-hex}`` so existing
    ``repo:tag`` and ``registry/repository`` parsing in the deploy path behaves
    exactly as it did for the old ``sregym/<db>-patched:<ver>-<hash8>`` tag.
    """
    digest = hashlib.sha256(f"{dockerfile_version}:{name}:{version}:{content_hash}".encode()).hexdigest()
    return f"{repo}:{digest[:16]}"
