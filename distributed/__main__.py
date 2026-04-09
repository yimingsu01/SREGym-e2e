"""Distributed SREGym runner — splits problems across CloudLab nodes and monitors via TUI.

Usage:
    uv run python -m distributed --hosts distributed_hosts.yaml --agent stratus --model gpt-5
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime

# Suppress noisy asyncssh/paramiko logging that bleeds through the TUI
logging.getLogger("asyncssh").setLevel(logging.WARNING)
logging.getLogger("paramiko").setLevel(logging.WARNING)

from distributed.app import DistributedRunnerApp
from distributed.config import load_nodes
from distributed.partitioner import filter_problems, partition_problems
from distributed.remote import RemoteNode


def get_problem_ids() -> list[str]:
    """Get all problem IDs from the local ProblemRegistry."""
    from sregym.conductor.problems.registry import ProblemRegistry

    registry = ProblemRegistry()
    return registry.get_problem_ids()


def build_run_id() -> str:
    return datetime.now().strftime("%m%d_%H%M")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Distribute SREGym problems across CloudLab nodes with a k9s-like TUI",
    )
    parser.add_argument(
        "--hosts",
        type=str,
        required=True,
        help="Path to distributed_hosts.yaml or ansible inventory.yml",
    )
    parser.add_argument(
        "--agent",
        type=str,
        default="stratus",
        help="Agent to run (default: stratus)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="gpt-5",
        help="LiteLLM model string",
    )
    parser.add_argument(
        "--judge-model",
        type=str,
        default=None,
        help="Model for LLM-as-a-judge evaluator",
    )
    parser.add_argument(
        "--n-attempts",
        type=int,
        default=1,
        help="Number of attempts per problem (default: 1)",
    )
    parser.add_argument(
        "--agent-timeout",
        type=int,
        default=1800,
        help="Agent timeout in seconds (default: 1800)",
    )
    parser.add_argument(
        "--strategy",
        type=str,
        default="round-robin",
        choices=["round-robin", "contiguous"],
        help="Problem partitioning strategy (default: round-robin)",
    )
    parser.add_argument(
        "--include",
        type=str,
        default=None,
        help="Comma-separated glob patterns to include (e.g., 'namespace_*,pvc_*')",
    )
    parser.add_argument(
        "--exclude",
        type=str,
        default=None,
        help="Comma-separated glob patterns to exclude",
    )
    parser.add_argument(
        "--noise",
        action="store_true",
        help="Enable transient noise injection",
    )
    parser.add_argument(
        "--force-build",
        action="store_true",
        help="Force rebuild agent Docker image on each node",
    )
    parser.add_argument(
        "--pod-timeout",
        type=int,
        default=1200,
        help="Timeout in seconds for pods to become ready (default: 1200)",
    )
    parser.add_argument(
        "--env",
        type=str,
        action="append",
        default=[],
        help='Environment variables to set on remote nodes, e.g. --env AWS_PROFILE=default --env AWS_DEFAULT_REGION=us-east-2',
    )
    parser.add_argument(
        "--attach",
        type=str,
        default=None,
        metavar="RUN_ID",
        help="Attach to an existing run (monitor only, no setup/launch). "
             "Pass the run ID shown in the TUI header (e.g. 0406_2023)",
    )
    args = parser.parse_args()

    # Load nodes
    node_configs = load_nodes(args.hosts)
    if not node_configs:
        print("Error: No nodes found in hosts file.", file=sys.stderr)
        sys.exit(1)

    attach_mode = args.attach is not None

    if attach_mode:
        # ── Attach mode: monitor an existing run, no setup/launch ──
        run_id = args.attach

        # Get problem list for display (best-effort, doesn't affect monitoring)
        try:
            problem_ids = get_problem_ids()
            problem_ids = filter_problems(problem_ids, include=args.include, exclude=args.exclude)
        except Exception:
            problem_ids = []

        n_nodes = len(node_configs)
        if problem_ids:
            buckets = partition_problems(problem_ids, n_nodes, strategy=args.strategy)
        else:
            # No local registry — assign empty lists, polling will discover progress
            buckets = [[] for _ in range(n_nodes)]

        remote_nodes: list[RemoteNode] = []
        problems_per_node: dict[str, list[str]] = {}
        for config, problems in zip(node_configs, buckets):
            node = RemoteNode(config)
            remote_nodes.append(node)
            problems_per_node[config.label] = problems

        print(f"Attaching to existing run: {run_id}")
        print(f"  Nodes: {n_nodes}")
        print()

        app = DistributedRunnerApp(
            nodes=remote_nodes,
            run_id=run_id,
            problems_per_node=problems_per_node,
            launch_config=None,  # No launch — just monitor
        )

    else:
        # ── Normal mode: partition, setup, launch ──
        # Get and filter problems
        problem_ids = get_problem_ids()
        problem_ids = filter_problems(problem_ids, include=args.include, exclude=args.exclude)
        if not problem_ids:
            print("Error: No problems to run after filtering.", file=sys.stderr)
            sys.exit(1)

        # Partition
        n_nodes = len(node_configs)
        buckets = partition_problems(problem_ids, n_nodes, strategy=args.strategy)

        # Build remote nodes and problem mapping
        remote_nodes = []
        problems_per_node = {}
        for config, problems in zip(node_configs, buckets):
            node = RemoteNode(config)
            remote_nodes.append(node)
            problems_per_node[config.label] = problems

        run_id = build_run_id()

        # Build extra args to forward
        extra_parts: list[str] = []
        if args.judge_model:
            extra_parts.append(f"--judge-model {args.judge_model}")
        if args.noise:
            extra_parts.append("--noise")
        if args.force_build:
            extra_parts.append("--force-build")
        extra_args = " ".join(extra_parts)

        # Parse --env KEY=VALUE pairs into a dict
        remote_env: dict[str, str] = {}
        for env_str in args.env:
            if "=" in env_str:
                k, v = env_str.split("=", 1)
                remote_env[k] = v
            else:
                print(f"Warning: ignoring malformed --env '{env_str}' (expected KEY=VALUE)", file=sys.stderr)

        # Stratus agent requires AWS credentials
        if args.agent == "stratus":
            remote_env.setdefault("AWS_PROFILE", "default")
            remote_env.setdefault("AWS_DEFAULT_REGION", "us-east-2")

        # Set pod readiness timeout
        remote_env["WAIT_FOR_POD_READY_TIMEOUT"] = str(args.pod_timeout)

        # Print summary before launching TUI
        print(f"Distributed SREGym Run: {run_id}")
        print(f"  Agent: {args.agent}  Model: {args.model}")
        print(f"  Problems: {len(problem_ids)} across {n_nodes} nodes ({args.strategy})")
        for config, problems in zip(node_configs, buckets):
            print(f"  {config.label} ({config.host}): {len(problems)} problems")
        print()

        app = DistributedRunnerApp(
            nodes=remote_nodes,
            run_id=run_id,
            problems_per_node=problems_per_node,
            launch_config={
                "agent": args.agent,
                "model": args.model,
                "n_attempts": args.n_attempts,
                "agent_timeout": args.agent_timeout,
                "extra_args": extra_args,
                "remote_env": remote_env,
            },
        )

    app.run()

    # Cleanup: disconnect (only remove tasklists if we launched)
    async def _cleanup():
        for node in remote_nodes:
            if not attach_mode:
                try:
                    await node.cleanup_tasklist()
                except Exception:
                    pass
            await node.disconnect()

    asyncio.run(_cleanup())


if __name__ == "__main__":
    main()
