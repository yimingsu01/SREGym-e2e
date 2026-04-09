from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class NodeState(str, Enum):
    PENDING = "pending"
    CONNECTING = "connecting"
    SETUP = "setup"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"

    @property
    def color(self) -> str:
        return {
            NodeState.PENDING: "dim",
            NodeState.CONNECTING: "cyan",
            NodeState.SETUP: "magenta",
            NodeState.RUNNING: "yellow",
            NodeState.DONE: "green",
            NodeState.ERROR: "red bold",
        }[self]

    @property
    def symbol(self) -> str:
        return {
            NodeState.PENDING: "○",
            NodeState.CONNECTING: "◌",
            NodeState.SETUP: "⚙",
            NodeState.RUNNING: "●",
            NodeState.DONE: "✓",
            NodeState.ERROR: "✗",
        }[self]


@dataclass
class ProblemResult:
    problem_id: str
    diagnosis_success: bool | None = None
    mitigation_success: bool | None = None

    @property
    def passed(self) -> bool | None:
        if self.mitigation_success is not None:
            return self.mitigation_success
        return self.diagnosis_success


@dataclass
class NodeStatus:
    label: str
    host: str
    state: NodeState = NodeState.PENDING
    assigned_count: int = 0
    completed_count: int = 0
    passed_count: int = 0
    failed_count: int = 0
    current_problem: str | None = None
    results: list[ProblemResult] = field(default_factory=list)
    error_message: str | None = None
    elapsed_seconds: float = 0.0

    @property
    def progress_str(self) -> str:
        return f"{self.completed_count}/{self.assigned_count}"

    @classmethod
    def from_poll_data(
        cls,
        data: dict,
        label: str,
        host: str,
        assigned_count: int,
        tmux_alive: bool,
        elapsed: float = 0.0,
    ) -> NodeStatus:
        """Build a NodeStatus from the CSV-based poll data returned by RemoteNode.poll_status().

        Args:
            data: dict with keys: completed, results, current_problem
            label: node label
            host: node hostname
            assigned_count: total problems assigned to this node
            tmux_alive: whether the tmux session is still running
            elapsed: seconds since run started
        """
        results = [
            ProblemResult(
                problem_id=r["problem_id"],
                diagnosis_success=_parse_bool(r.get("diagnosis_success")),
                mitigation_success=_parse_bool(r.get("mitigation_success")),
            )
            for r in data.get("results", [])
        ]
        passed = sum(1 for r in results if r.passed is True)
        failed = sum(1 for r in results if r.passed is False)
        completed_count = len(data.get("completed", []))

        # Determine state
        if completed_count >= assigned_count and not tmux_alive:
            state = NodeState.DONE
        elif tmux_alive:
            state = NodeState.RUNNING
        elif completed_count < assigned_count:
            state = NodeState.ERROR
        else:
            state = NodeState.DONE

        return cls(
            label=label,
            host=host,
            state=state,
            assigned_count=assigned_count,
            completed_count=completed_count,
            passed_count=passed,
            failed_count=failed,
            current_problem=data.get("current_problem"),
            results=results,
            elapsed_seconds=elapsed,
        )


def _parse_bool(value) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.lower().strip()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no", "none", ""):
            return False
    return bool(value)
