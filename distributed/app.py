from __future__ import annotations

import asyncio
import subprocess
import time

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from distributed.collector import collect_all
from distributed.remote import RemoteNode
from distributed.status import NodeState, NodeStatus


def _short_error(e: Exception) -> str:
    """Extract a human-readable error message from an exception."""
    import asyncssh

    if isinstance(e, asyncssh.process.ProcessError):
        # Show the command that failed and its stderr
        cmd = e.command or "unknown command"
        # Truncate long commands to the first useful part
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        stderr = (e.stderr or "").strip()
        if stderr:
            # Take last line of stderr (usually the most useful)
            last_line = stderr.strip().splitlines()[-1]
            return f"Command failed: {last_line}"
        return f"Command exited {e.exit_status}: {cmd}"
    if isinstance(e, asyncssh.misc.DisconnectError):
        return f"SSH disconnected: {e.reason}"
    if isinstance(e, OSError):
        return f"Connection error: {e}"
    msg = str(e)
    if len(msg) > 120:
        msg = msg[:117] + "..."
    return msg


class StatusUpdated(Message):
    """Posted when a node's status has been polled."""

    def __init__(self, label: str, status: NodeStatus) -> None:
        super().__init__()
        self.label = label
        self.status = status


class DistributedRunnerApp(App):
    """k9s-like TUI for monitoring distributed SREGym runs."""

    CSS = """
    #stats {
        height: 3;
        background: $surface;
        padding: 0 2;
        content-align: left middle;
    }
    #node-table {
        height: 1fr;
        min-height: 8;
    }
    #log-pane {
        height: 1fr;
        min-height: 6;
        border-top: solid $primary;
    }
    """

    BINDINGS = [
        Binding("q", "quit_app", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("c", "collect", "Collect Results"),
        Binding("a", "abort_node", "Abort Node"),
        Binding("enter", "view_logs", "View Logs"),
        Binding("escape", "clear_logs", "Clear Logs"),
        Binding("t", "attach_tmux", "Tmux"),
        Binding("i", "attach_install", "Install Log"),
        Binding("s", "attach_shell", "Shell"),
        Binding("p", "show_problems", "Problems"),
    ]

    def __init__(
        self,
        nodes: list[RemoteNode],
        run_id: str,
        problems_per_node: dict[str, list[str]],
        launch_config: dict | None = None,
    ):
        super().__init__()
        self.nodes = {n.label: n for n in nodes}
        self.run_id = run_id
        self.problems_per_node = problems_per_node
        self.launch_config = launch_config or {}
        self._statuses: dict[str, NodeStatus] = {}
        self._start_times: dict[str, float] = {}
        self._selected_node: str | None = None
        self._polling = True

        # Initialize statuses
        for label, node in self.nodes.items():
            self._statuses[label] = NodeStatus(
                label=label,
                host=node.config.host,
                state=NodeState.PENDING,
                assigned_count=len(problems_per_node.get(label, [])),
            )

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="stats")
        yield DataTable(id="node-table", cursor_type="row")
        yield RichLog(id="log-pane", highlight=True, markup=True, max_lines=1000)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#node-table", DataTable)
        table.add_columns("Node", "Status", "Progress", "Pass", "Fail", "Current Problem", "Time")
        self._refresh_table()
        self._update_stats()
        if self.launch_config:
            self._launch_nodes()
        else:
            # Attach mode: just connect SSH and start monitoring
            self._connect_only()
        self.start_polling()

    @work(exclusive=True, group="connector")
    async def _connect_only(self) -> None:
        """Attach mode: connect SSH to all nodes without launching anything."""
        async def _connect_one(label: str, node: RemoteNode) -> None:
            self.mark_connecting(label)
            self.write_log(f"[cyan]{label}:[/] Connecting to {node.config.host}...")
            try:
                await node.connect()
                self.write_log(f"[green]{label}:[/] Connected (attach mode)")
                # Check if tmux session is alive
                alive = await node.is_tmux_alive(self.run_id)
                if alive:
                    self.mark_running(label)
                    self.write_log(f"[green]{label}:[/] tmux session sregym-{self.run_id} is running")
                else:
                    self.mark_error(label, f"tmux sregym-{self.run_id} not found")
                    self.write_log(f"[yellow]{label}:[/] tmux session sregym-{self.run_id} not found")
            except Exception as e:
                msg = f"SSH failed: {_short_error(e)}"
                self.mark_error(label, msg)
                self.write_log(f"[red]{label}:[/] {msg}")

        await asyncio.gather(*[
            _connect_one(label, node) for label, node in self.nodes.items()
        ])

    @work(exclusive=True, group="launcher")
    async def _launch_nodes(self) -> None:
        """Connect to all nodes and start runs in parallel."""
        tasks = [
            self._launch_one(label, node)
            for label, node in self.nodes.items()
        ]
        await asyncio.gather(*tasks)

    async def _launch_one(self, label: str, node: RemoteNode) -> None:
        """Connect and set up a single node."""
        cfg = self.launch_config

        # Step 1: SSH connect
        self.mark_connecting(label)
        self.write_log(f"[cyan]{label}:[/] Connecting to {node.config.host}...")
        try:
            await node.connect()
            self.write_log(f"[green]{label}:[/] SSH connected")
        except Exception as e:
            msg = f"SSH failed: {_short_error(e)}"
            self.mark_error(label, msg)
            self.write_log(f"[red]{label}:[/] {msg}")
            return

        problems = self.problems_per_node.get(label, [])
        if not problems:
            self.mark_error(label, "No problems assigned")
            return

        # Step 2+: start_run (multi-step setup)
        self.mark_setup(label, "starting setup...")
        try:
            await node.start_run(
                problems=problems,
                agent=cfg["agent"],
                model=cfg["model"],
                run_id=self.run_id,
                n_attempts=cfg.get("n_attempts", 1),
                agent_timeout=cfg.get("agent_timeout", 1800),
                extra_args=cfg.get("extra_args", ""),
                remote_env=cfg.get("remote_env", {}),
                progress_callback=lambda step, lbl=label: self._on_setup_step(lbl, step),
            )
            self.mark_running(label)
            self.write_log(f"[green]{label}:[/] Run started successfully")
        except Exception as e:
            msg = _short_error(e)
            self.mark_error(label, msg)
            self.write_log(f"[red]{label}:[/] {msg}")

    def _on_setup_step(self, label: str, step: str) -> None:
        """Callback from RemoteNode.start_run to report setup progress."""
        self.mark_setup(label, step)
        self.write_log(f"[magenta]{label}:[/] {step}")

    # ── Polling ────────────────────────────────────────────────

    @work(exclusive=True, group="poller")
    async def start_polling(self) -> None:
        """Background worker that polls all nodes every 3 seconds."""
        while self._polling:
            tasks = []
            for label, node in self.nodes.items():
                tasks.append(self._poll_one(label, node))
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(3)

    async def _poll_one(self, label: str, node: RemoteNode) -> None:
        try:
            if node.conn is None:
                return

            current_state = self._statuses[label].state
            # Don't poll nodes that are still being set up, already done, or in error
            if current_state in (NodeState.PENDING, NodeState.CONNECTING, NodeState.SETUP, NodeState.DONE, NodeState.ERROR):
                return

            tmux_alive = await node.is_tmux_alive(self.run_id)
            data = await node.poll_status()
            assigned = len(self.problems_per_node.get(label, []))

            if data:
                elapsed = time.time() - self._start_times.get(label, time.time())
                status = NodeStatus.from_poll_data(
                    data, label, node.config.host, assigned, tmux_alive, elapsed
                )
                self.post_message(StatusUpdated(label, status))
            elif not tmux_alive:
                # tmux died without producing any results
                current = self._statuses[label]
                current.state = NodeState.ERROR
                current.error_message = "tmux session sregym-{} not found".format(self.run_id)
                self.post_message(StatusUpdated(label, current))
        except Exception as e:
            current = self._statuses[label]
            current.state = NodeState.ERROR
            current.error_message = str(e)
            self.post_message(StatusUpdated(label, current))

    @on(StatusUpdated)
    def handle_status_updated(self, event: StatusUpdated) -> None:
        self._statuses[event.label] = event.status
        self._refresh_table()
        self._update_stats()

    # ── Table rendering ────────────────────────────────────────

    def _refresh_table(self) -> None:
        table = self.query_one("#node-table", DataTable)
        table.clear()

        for label in sorted(self._statuses):
            s = self._statuses[label]
            state_str = f"[{s.state.color}]{s.state.symbol} {s.state.value.upper()}[/]"
            progress = s.progress_str
            passed = f"[green]{s.passed_count}[/]" if s.passed_count else "0"
            failed = f"[red]{s.failed_count}[/]" if s.failed_count else "0"

            # Show error message instead of current problem when in error state
            if s.state == NodeState.ERROR and s.error_message:
                current = f"[red]{s.error_message}[/]"
                if len(s.error_message) > 45:
                    current = f"[red]{s.error_message[:42]}...[/]"
            else:
                current = s.current_problem or "--"
                if len(current) > 30:
                    current = current[:27] + "..."

            elapsed = self._format_elapsed(s.elapsed_seconds)

            table.add_row(
                label,
                state_str,
                progress,
                passed,
                failed,
                current,
                elapsed,
                key=label,
            )

    def _update_stats(self) -> None:
        total_assigned = sum(s.assigned_count for s in self._statuses.values())
        total_completed = sum(s.completed_count for s in self._statuses.values())
        total_passed = sum(s.passed_count for s in self._statuses.values())
        total_failed = sum(s.failed_count for s in self._statuses.values())
        n_nodes = len(self._statuses)
        n_done = sum(1 for s in self._statuses.values() if s.state == NodeState.DONE)
        n_error = sum(1 for s in self._statuses.values() if s.state == NodeState.ERROR)

        stats_text = (
            f" [bold]SREGym Distributed Runner[/]  "
            f"[dim]run:[/] {self.run_id}  "
            f"[dim]nodes:[/] {n_nodes} ({n_done} done, {n_error} err)  │  "
            f"[dim]problems:[/] {total_completed}/{total_assigned}  "
            f"[green]passed: {total_passed}[/]  "
            f"[red]failed: {total_failed}[/]"
        )
        self.query_one("#stats", Static).update(stats_text)

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        if seconds <= 0:
            return "--"
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    # ── Key bindings ───────────────────────────────────────────

    def _get_selected_label(self) -> str | None:
        table = self.query_one("#node-table", DataTable)
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            return str(row_key.value) if row_key else None
        except Exception:
            return None

    def action_quit_app(self) -> None:
        self._polling = False
        self.exit()

    def action_refresh(self) -> None:
        self._refresh_table()
        self._update_stats()

    @work(exclusive=True, group="collect")
    async def action_collect(self) -> None:
        log = self.query_one("#log-pane", RichLog)
        log.write("[bold cyan]Collecting results from all nodes...[/]")
        try:
            merged = await collect_all(list(self.nodes.values()), self.run_id)
            log.write(f"[bold green]Results merged to: {merged}[/]")
        except Exception as e:
            log.write(f"[bold red]Collection failed: {e}[/]")

    @work(exclusive=True, group="abort")
    async def action_abort_node(self) -> None:
        label = self._get_selected_label()
        if not label or label not in self.nodes:
            return
        log = self.query_one("#log-pane", RichLog)
        log.write(f"[bold red]Aborting {label}...[/]")
        try:
            await self.nodes[label].abort_run(self.run_id)
            self._statuses[label].state = NodeState.ERROR
            self._statuses[label].error_message = "Aborted by user"
            self._refresh_table()
            self._update_stats()
            log.write(f"[bold red]Aborted {label}[/]")
        except Exception as e:
            log.write(f"[red]Failed to abort {label}: {e}[/]")

    @work(exclusive=True, group="log-stream")
    async def action_view_logs(self) -> None:
        label = self._get_selected_label()
        if not label or label not in self.nodes:
            return

        # Stop previous stream
        if self._selected_node and self._selected_node in self.nodes:
            await self.nodes[self._selected_node].stop_log_stream()

        self._selected_node = label
        log = self.query_one("#log-pane", RichLog)
        log.clear()
        log.write(f"[bold cyan]── Logs: {label} ──[/]\n")

        node = self.nodes[label]
        try:
            await node.start_log_stream(self.run_id, lambda line: log.write(line))
        except Exception as e:
            log.write(f"[red]Failed to stream logs from {label}: {e}[/]")

    @work(exclusive=False, group="clear-logs")
    async def action_clear_logs(self) -> None:
        log = self.query_one("#log-pane", RichLog)
        log.clear()
        if self._selected_node and self._selected_node in self.nodes:
            await self.nodes[self._selected_node].stop_log_stream()
        self._selected_node = None

    def action_show_problems(self) -> None:
        """Show the problem list for the selected node in the log pane."""
        label = self._get_selected_label()
        if not label:
            return

        log = self.query_one("#log-pane", RichLog)
        log.clear()
        log.write(f"[bold cyan]── Problems: {label} ──[/]\n")

        problems = self.problems_per_node.get(label, [])
        if not problems:
            log.write("[dim]No problems assigned[/]")
            return

        # Build sets of completed/passed/failed from status
        status = self._statuses.get(label)
        completed_ids: set[str] = set()
        passed_ids: set[str] = set()
        failed_ids: set[str] = set()
        current_problem = None

        if status:
            current_problem = status.current_problem
            for r in status.results:
                completed_ids.add(r.problem_id)
                if r.passed is True:
                    passed_ids.add(r.problem_id)
                elif r.passed is False:
                    failed_ids.add(r.problem_id)

        for i, pid in enumerate(problems, 1):
            if pid in passed_ids:
                icon = "[green]PASS[/]"
            elif pid in failed_ids:
                icon = "[red]FAIL[/]"
            elif pid == current_problem:
                icon = "[yellow] RUN[/]"
            elif pid in completed_ids:
                icon = "[blue]DONE[/]"
            else:
                icon = "[dim] ...[/]"
            log.write(f"  {icon}  {i:3d}. {pid}")

        n_done = len(completed_ids)
        n_pass = len(passed_ids)
        n_fail = len(failed_ids)
        log.write(
            f"\n[dim]Total: {len(problems)}  Done: {n_done}  "
            f"[green]Passed: {n_pass}[/]  [red]Failed: {n_fail}[/][/]"
        )

    # ── Interactive terminal (suspend TUI, SSH in) ──────────────

    def _ssh_cmd(self, label: str) -> list[str] | None:
        """Build the base SSH command for a node."""
        if label not in self.nodes:
            return None
        node = self.nodes[label]
        cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-t"]
        if node.config.ssh_key:
            cmd += ["-i", node.config.ssh_key]
        if node.config.port != 22:
            cmd += ["-p", str(node.config.port)]
        cmd.append(f"{node.config.user}@{node.config.host}")
        return cmd

    def action_attach_tmux(self) -> None:
        """Suspend TUI and attach to the main sregym tmux session on the selected node."""
        label = self._get_selected_label()
        if not label:
            return
        ssh = self._ssh_cmd(label)
        if not ssh:
            return
        session = f"sregym-{self.run_id}"
        ssh.append(
            f"tmux attach -t {session} 2>/dev/null || "
            f"{{ echo 'Session {session} not found. Available sessions:'; tmux ls 2>/dev/null || echo '(none)'; "
            f"echo; echo 'Press enter to return...'; read; }}"
        )
        self._run_interactive(label, ssh, f"tmux:{session}")

    def action_attach_install(self) -> None:
        """Suspend TUI and show install/setup logs or attach to live session."""
        label = self._get_selected_label()
        if not label:
            return
        ssh = self._ssh_cmd(label)
        if not ssh:
            return
        # Try to attach to live session; if gone, show marker file status and recent logs
        node = self.nodes[label]
        sregym = node.config.sregym_path
        ssh.append(
            "tmux attach -t installations 2>/dev/null || "
            "tmux attach -t setup_env 2>/dev/null || "
            "{ echo '=== Install/setup sessions already exited ==='; echo; "
            "echo '--- Marker files ---'; "
            "ls -la ~/.sregym_install_done ~/.sregym_install_fail "
            "~/.sregym_setup_done ~/.sregym_setup_fail 2>/dev/null || echo '(no markers found)'; "
            "echo; echo '--- Last 50 lines of install output ---'; "
            "tail -50 ~/.sregym_install.log 2>/dev/null || echo '(no install log)'; "
            "echo; echo '--- Last 50 lines of setup output ---'; "
            "tail -50 ~/.sregym_setup.log 2>/dev/null || echo '(no setup log)'; "
            "echo; echo 'Press enter to return...'; read; }"
        )
        self._run_interactive(label, ssh, "install logs")

    def action_attach_shell(self) -> None:
        """Suspend TUI and open a plain SSH shell on the selected node."""
        label = self._get_selected_label()
        if not label:
            return
        ssh = self._ssh_cmd(label)
        if not ssh:
            return
        self._run_interactive(label, ssh, "shell")

    def _run_interactive(self, label: str, cmd: list[str], desc: str) -> None:
        """Suspend the TUI, run an interactive command, resume on exit."""
        with self.suspend():
            print(f"\033[1m── Entering {desc} on {label} ──\033[0m")
            print(f"\033[2m   Detach tmux: Ctrl+B then D  |  Exit shell: exit\033[0m\n")
            subprocess.run(cmd)
            print(f"\n\033[1m── Returned to TUI ──\033[0m")

    # ── Lifecycle ──────────────────────────────────────────────

    def set_start_time(self, label: str) -> None:
        self._start_times[label] = time.time()

    def mark_connecting(self, label: str) -> None:
        if label in self._statuses:
            self._statuses[label].state = NodeState.CONNECTING
            self._refresh_table()
            self._update_stats()

    def mark_running(self, label: str) -> None:
        if label in self._statuses:
            self._statuses[label].state = NodeState.RUNNING
            self.set_start_time(label)
            self._refresh_table()
            self._update_stats()

    def mark_setup(self, label: str, step: str = "") -> None:
        if label in self._statuses:
            self._statuses[label].state = NodeState.SETUP
            if step:
                self._statuses[label].current_problem = step
            self._refresh_table()
            self._update_stats()

    def mark_error(self, label: str, msg: str) -> None:
        if label in self._statuses:
            self._statuses[label].state = NodeState.ERROR
            self._statuses[label].error_message = msg
            self._refresh_table()
            self._update_stats()

    def write_log(self, text: str) -> None:
        """Write a line to the log pane."""
        try:
            log_widget = self.query_one("#log-pane", RichLog)
            log_widget.write(text)
        except Exception:
            pass
