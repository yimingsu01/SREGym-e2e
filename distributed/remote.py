from __future__ import annotations

import asyncio
import csv
import io
import logging
from pathlib import Path
from typing import Callable

import asyncssh

from distributed.config import NodeConfig

logger = logging.getLogger(__name__)

TASKLIST_REL = "conductor/tasklist.yml"


def _build_tasklist_yaml(problem_ids: list[str]) -> str:
    lines = ["all:", "  problems:"]
    for pid in problem_ids:
        lines.append(f"    {pid}:")
        lines.append("    - diagnosis")
        lines.append("    - mitigation")
    return "\n".join(lines) + "\n"


class RemoteNode:
    def __init__(self, config: NodeConfig):
        self.config = config
        self.conn: asyncssh.SSHClientConnection | None = None
        self._log_process: asyncssh.SSHClientProcess | None = None
        self._log_task: asyncio.Task | None = None
        self._assigned_problems: list[str] = []

    @property
    def label(self) -> str:
        return self.config.label

    async def connect(self) -> None:
        connect_kwargs: dict = {
            "host": self.config.host,
            "port": self.config.port,
            "username": self.config.user,
            "known_hosts": None,
            "keepalive_interval": 30,
            "keepalive_count_max": 10,
        }
        if self.config.ssh_key:
            connect_kwargs["client_keys"] = [self.config.ssh_key]

        self.conn = await asyncssh.connect(**connect_kwargs)
        logger.info(f"Connected to {self.label} ({self.config.host})")

    async def disconnect(self) -> None:
        await self.stop_log_stream()
        if self.conn:
            self.conn.close()
            self.conn = None

    async def _ensure_connected(self) -> None:
        """Reconnect if the SSH connection has dropped."""
        try:
            if self.conn is not None:
                # Quick liveness check
                await self.conn.run("true", check=False, timeout=10)
                return
        except Exception:
            logger.warning(f"Connection to {self.label} lost, reconnecting...")
            self.conn = None

        await self.connect()

    async def _run_step(self, cmd: str, step_name: str) -> asyncssh.SSHCompletedProcess:
        """Run a remote command, reconnecting if needed, raising on failure."""
        await self._ensure_connected()
        assert self.conn is not None
        result = await self.conn.run(cmd, check=False)
        if result.exit_status != 0:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            detail = stderr or stdout or f"exit code {result.exit_status}"
            # Take the last meaningful line
            last_line = detail.strip().splitlines()[-1] if detail else ""
            raise RuntimeError(f"{step_name}: {last_line}")
        return result

    async def _wait_for_marker(
        self, phase: str, label: str, prefix: str, max_checks: int = 360, interval: int = 5
    ) -> None:
        """Poll for marker files ({prefix}_done / {prefix}_fail)."""
        for _ in range(max_checks):
            await self._ensure_connected()
            assert self.conn is not None
            r = await self.conn.run(
                f"test -f {prefix}_done && echo done || "
                f"(test -f {prefix}_fail && echo fail || echo waiting)",
                check=False,
            )
            status = (r.stdout or "").strip()
            if status == "done":
                return
            if status == "fail":
                log_r = await self.conn.run(
                    f"tail -5 {prefix}.log 2>/dev/null", check=False
                )
                log_tail = (log_r.stdout or "").strip()
                hint = f" — {log_tail.splitlines()[-1]}" if log_tail else ""
                raise RuntimeError(f"{label} failed{hint} — press 'i' for full log")
            await asyncio.sleep(interval)
        raise RuntimeError(f"{label} timed out after {max_checks * interval}s")

    async def start_run(
        self,
        problems: list[str],
        agent: str,
        model: str,
        run_id: str,
        n_attempts: int = 1,
        agent_timeout: int = 1800,
        extra_args: str = "",
        remote_env: dict[str, str] | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        assert self.conn is not None
        self._assigned_problems = list(problems)

        def _report(step: str) -> None:
            if progress_callback:
                progress_callback(step)

        tmux_session = f"sregym-{run_id}"
        log_file = f"logs/distributed_{run_id}.log"

        # Step 1: SCP node_setup.py to the remote node
        _report("Copying setup script...")
        local_setup = Path(__file__).parent / "node_setup.py"
        if not local_setup.exists():
            raise RuntimeError(f"node_setup.py not found at {local_setup}")
        await asyncssh.scp(str(local_setup), (self.conn, "~/node_setup.py"))

        # Step 2: Clone repo (with submodules) if needed, then pull latest
        _report("Cloning/updating repo...")
        sregym_parent = str(Path(self.config.sregym_path).parent)
        await self._run_step(
            f"mkdir -p {sregym_parent} && "
            f"if [ ! -d {self.config.sregym_path} ]; then "
            f"git clone --recurse-submodules https://github.com/SREGym/SREGym.git {self.config.sregym_path}; "
            f"fi && "
            f"cd {self.config.sregym_path} && "
            f"git pull --ff-only 2>/dev/null || true && "
            f"git submodule update --init --recursive",
            "Git clone/pull",
        )

        sregym = self.config.sregym_path
        setup_cmd = f"python3 ~/node_setup.py --sregym-path {sregym}"

        # Step 3: Run installations (brew, go, docker, kind, python, git, kubectl)
        _report("Running installations...")
        await self._run_step(
            f"rm -f ~/.sregym_install_done ~/.sregym_install_fail && "
            f"tmux kill-session -t installations 2>/dev/null || true && "
            f"tmux new-session -d -s installations "
            f"'bash -lc \"{setup_cmd} --install "
            f"2>&1 | tee ~/.sregym_install.log "
            f"&& touch ~/.sregym_install_done "
            f"|| touch ~/.sregym_install_fail\"'",
            "Start installations",
        )

        # Step 4: Wait for installations to finish
        _report("Waiting for installations to complete...")
        await self._wait_for_marker(
            "install", "Installations", "~/.sregym_install"
        )

        # Step 5: Run environment setup (uv, venv, uv sync)
        _report("Setting up Python environment (uv sync)...")
        await self._run_step(
            f"rm -f ~/.sregym_setup_done ~/.sregym_setup_fail && "
            f"tmux kill-session -t setup_env 2>/dev/null || true && "
            f"tmux new-session -d -s setup_env "
            f"'bash -lc \"{setup_cmd} --setup-env "
            f"2>&1 | tee ~/.sregym_setup.log "
            f"&& touch ~/.sregym_setup_done "
            f"|| touch ~/.sregym_setup_fail\"'",
            "Start setup-env",
        )

        # Step 6: Wait for setup-env to finish
        _report("Waiting for environment setup...")
        await self._wait_for_marker(
            "setup", "Setup", "~/.sregym_setup"
        )

        # Step 7: Create kind cluster (if not already running)
        _report("Creating kind cluster...")
        await self._run_step(
            f"rm -f ~/.sregym_cluster_done ~/.sregym_cluster_fail && "
            f"tmux kill-session -t cluster 2>/dev/null || true && "
            f"tmux new-session -d -s cluster "
            f"'bash -lc \"python3 ~/node_setup.py --create-cluster --sregym-path {sregym} "
            f"2>&1 | tee ~/.sregym_cluster.log "
            f"&& touch ~/.sregym_cluster_done "
            f"|| touch ~/.sregym_cluster_fail\"'",
            "Start cluster creation",
        )
        await self._wait_for_marker(
            "cluster", "Cluster creation", "~/.sregym_cluster", max_checks=120, interval=5
        )

        # Step 8: Patch main.py — convert deploy-failure crash to skip
        await self._run_step(
            f"python3 ~/node_setup.py --patch-main --sregym-path {sregym}",
            "Patch main.py",
        )

        # Step 9: Verify sregym_path exists and has main.py
        _report("Verifying SREGym installation...")
        await self._run_step(
            f"test -f {sregym}/main.py",
            f"main.py not found at {sregym}",
        )

        # Step 9: Verify .venv exists
        await self._run_step(
            f"test -d {sregym}/.venv",
            f".venv not found at {sregym} (uv sync may have failed)",
        )

        # Step 10: Upload tasklist.yml (pipe through stdin to avoid shell quoting issues)
        _report(f"Uploading tasklist ({len(problems)} problems)...")
        tasklist_content = _build_tasklist_yaml(problems)
        tasklist_path = f"{sregym}/{TASKLIST_REL}"
        # Ensure conductor/ directory exists
        await self._run_step(
            f"mkdir -p {sregym}/conductor",
            "Create conductor directory",
        )
        result = await self.conn.run(
            f"cat > {tasklist_path}",
            input=tasklist_content,
            check=False,
        )
        if result.exit_status != 0:
            stderr = (result.stderr or "").strip()
            raise RuntimeError(
                f"Failed to write tasklist to {tasklist_path}: {stderr or f'exit {result.exit_status}'}"
            )

        # Step 10: Kill stale processes from previous runs, clean CSVs, ensure logs dir
        await self._run_step(
            f"tmux kill-server 2>/dev/null || true; "
            f"fuser -k 8000/tcp 2>/dev/null || true; "
            f"fuser -k 6443/tcp 2>/dev/null || true; "
            f"cd {sregym} && mkdir -p logs && rm -f _running_*_results.csv",
            "Prepare run directory",
        )

        # Step 11: Launch main.py in tmux
        _report("Launching main.py...")
        # Write a launcher script to avoid shell quoting issues
        env_lines = ""
        if remote_env:
            env_lines = "\n".join(f"export {k}={v}" for k, v in remote_env.items())
        launcher_script = (
            f"#!/bin/bash\n"
            f'eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv 2>/dev/null)"\n'
            f"export KUBECONFIG=$HOME/.kube/config\n"
            f"cd {sregym}\n"
            f"source .venv/bin/activate\n"
            f"{env_lines}\n"
            f"python main.py "
            f"--agent {agent} "
            f"--model {model} "
            f"--n-attempts {n_attempts} "
            f"--agent-timeout {agent_timeout} "
            f"{extra_args} "
            f"2>&1 | tee {log_file}\n"
            f"sleep 300\n"
        )
        launcher_path = f"{sregym}/run_distributed.sh"
        result = await self.conn.run(
            f"cat > {launcher_path} && chmod +x {launcher_path}",
            input=launcher_script,
            check=False,
        )
        if result.exit_status != 0:
            raise RuntimeError(f"Failed to write launcher script: {(result.stderr or '').strip()}")

        tmux_cmd = (
            f"tmux new-session -d -s {tmux_session} 'bash {launcher_path}'"
        )
        await self._run_step(tmux_cmd, "Failed to start tmux session")

        # Step 12: Verify tmux actually started
        alive = await self.is_tmux_alive(run_id)
        if not alive:
            raise RuntimeError(
                f"tmux session sregym-{run_id} failed to start — "
                f"check {self.config.sregym_path}/{log_file} on the node"
            )

        logger.info(f"Started tmux session '{tmux_session}' on {self.label}")

    async def poll_status(self) -> dict | None:
        assert self.conn is not None

        csv_result = await self.conn.run(
            f"cat {self.config.sregym_path}/_running_*_results.csv 2>/dev/null",
            check=False,
        )

        completed: list[str] = []
        results: list[dict] = []

        if csv_result.exit_status == 0 and csv_result.stdout and csv_result.stdout.strip():
            try:
                reader = csv.DictReader(io.StringIO(csv_result.stdout))
                for row in reader:
                    pid = row.get("problem_id", "")
                    if pid:
                        completed.append(pid)
                        results.append({
                            "problem_id": pid,
                            "diagnosis_success": row.get("Diagnosis.success"),
                            "mitigation_success": row.get("Mitigation.success"),
                        })
            except Exception:
                pass

        current_problem = None

        log_result = await self.conn.run(
            f"grep -oP '(?<=Starting problem: )\\S+' "
            f"{self.config.sregym_path}/logs/distributed_*.log 2>/dev/null | tail -1",
            check=False,
        )

        if log_result.exit_status == 0 and log_result.stdout and log_result.stdout.strip():
            current_problem = log_result.stdout.strip().split()[0]

        if not completed and current_problem is None:
            return None

        return {
            "completed": completed,
            "results": results,
            "current_problem": current_problem,
        }

    async def is_tmux_alive(self, run_id: str) -> bool:
        assert self.conn is not None
        result = await self.conn.run(
            f"tmux has-session -t sregym-{run_id} 2>/dev/null",
            check=False,
        )
        return result.exit_status == 0

    async def start_log_stream(self, run_id: str, callback: Callable[[str], None]) -> None:
        assert self.conn is not None
        await self.stop_log_stream()

        log_path = f"{self.config.sregym_path}/logs/distributed_{run_id}.log"
        await self.conn.run(f"touch {log_path}", check=False)

        self._log_process = await self.conn.create_process(
            f"tail -n 50 -f {log_path}"
        )

        async def _reader():
            try:
                assert self._log_process is not None
                async for line in self._log_process.stdout:
                    callback(line.rstrip("\n"))
            except (asyncssh.BreakReceived, asyncssh.ConnectionLost, asyncio.CancelledError):
                pass

        self._log_task = asyncio.ensure_future(_reader())

    async def stop_log_stream(self) -> None:
        if self._log_task and not self._log_task.done():
            self._log_task.cancel()
            self._log_task = None
        if self._log_process:
            try:
                self._log_process.kill()
            except (OSError, asyncssh.ConnectionLost):
                pass
            self._log_process = None

    async def abort_run(self, run_id: str) -> None:
        assert self.conn is not None
        await self.conn.run(
            f"tmux kill-session -t sregym-{run_id} 2>/dev/null",
            check=False,
        )
        logger.info(f"Aborted tmux session on {self.label}")

    async def cleanup_tasklist(self) -> None:
        assert self.conn is not None
        await self.conn.run(
            f"rm -f {self.config.sregym_path}/{TASKLIST_REL}",
            check=False,
        )

    async def collect_results(self, local_dir: Path) -> None:
        assert self.conn is not None
        node_dir = local_dir / self.config.label
        node_dir.mkdir(parents=True, exist_ok=True)

        remote_results = f"{self.config.sregym_path}/results/"

        try:
            await asyncssh.scp(
                (self.conn, remote_results),
                str(node_dir),
                recurse=True,
            )
            logger.info(f"Collected results from {self.label} to {node_dir}")
        except (OSError, asyncssh.SFTPError) as e:
            logger.error(f"Failed to collect results from {self.label}: {e}")