from __future__ import annotations

import fnmatch


def partition_problems(
    problem_ids: list[str],
    n_nodes: int,
    strategy: str = "round-robin",
) -> list[list[str]]:
    """Split problem IDs across N nodes.

    Args:
        problem_ids: Full list of problem IDs to distribute.
        n_nodes: Number of nodes to split across.
        strategy: "round-robin" (default) or "contiguous".

    Returns:
        List of lists, one per node, containing that node's problem IDs.
    """
    if n_nodes <= 0:
        raise ValueError("n_nodes must be positive")
    if not problem_ids:
        return [[] for _ in range(n_nodes)]

    buckets: list[list[str]] = [[] for _ in range(n_nodes)]

    if strategy == "round-robin":
        for i, pid in enumerate(problem_ids):
            buckets[i % n_nodes].append(pid)
    elif strategy == "contiguous":
        chunk_size = len(problem_ids) // n_nodes
        remainder = len(problem_ids) % n_nodes
        start = 0
        for i in range(n_nodes):
            end = start + chunk_size + (1 if i < remainder else 0)
            buckets[i] = problem_ids[start:end]
            start = end
    else:
        raise ValueError(f"Unknown strategy: {strategy}. Use 'round-robin' or 'contiguous'.")

    return buckets


def filter_problems(
    problem_ids: list[str],
    include: str | None = None,
    exclude: str | None = None,
) -> list[str]:
    """Filter problem IDs by glob patterns.

    Args:
        problem_ids: Full list of problem IDs.
        include: Comma-separated glob patterns to include (e.g., "namespace_*,pvc_*").
        exclude: Comma-separated glob patterns to exclude.

    Returns:
        Filtered list of problem IDs.
    """
    result = list(problem_ids)

    if include:
        patterns = [p.strip() for p in include.split(",")]
        result = [pid for pid in result if any(fnmatch.fnmatch(pid, pat) for pat in patterns)]

    if exclude:
        patterns = [p.strip() for p in exclude.split(",")]
        result = [pid for pid in result if not any(fnmatch.fnmatch(pid, pat) for pat in patterns)]

    return result
