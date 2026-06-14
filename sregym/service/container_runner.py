import contextlib
import json
import logging
import os
import platform
import socket
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("all.sregym.container_runner")

# Hostname the agent container uses to reach host-side control-plane services
# (conductor API, MCP server, filtered Kubernetes API proxy) when it is NOT on
# the host network. Resolved to the host via ``--add-host=...:host-gateway``.
CONTROL_PLANE_HOST = "host.docker.internal"

# Egress modes for the agent container.
EGRESS_OPEN = "open"  # default: unrestricted (current behavior)
EGRESS_RESTRICTED = "restricted"  # only control plane + LLM API endpoints allowed


@dataclass
class ExecInput:
    command: str
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    timeout: int | None = None  # seconds, None = no timeout
    label: str = ""
    container_name: str = ""


@dataclass
class ContainerConfig:
    image: str = "sregym-agent-base:latest"
    network_mode: str = "host"
    kubeconfig_path: Path | None = None
    workspace_path: Path | None = None  # bind-mounted to /workspace for agent output
    logs_path: Path | None = None
    sregym_apps_path: Path | None = None
    sregym_app_subdirs: list[str] | None = None
    source_code_path: Path | None = None  # bind-mounted to /opt/source:rw for code-level bug fixing
    env_vars: dict = field(default_factory=dict)
    cpus: float = 4.0
    memory: str = "8g"
    # Egress policy. "open" (default) keeps the existing unrestricted behavior.
    # "restricted" puts the agent on a dedicated bridge network and (best-effort)
    # firewalls its egress down to the host control plane + the LLM API endpoints,
    # so an agent cannot re-clone upstream source, browse the issue/PR, or web-search
    # the fix for a code-level database bug.
    egress_mode: str = EGRESS_OPEN
    # Extra hostnames/IPs the restricted agent is allowed to reach, in addition to
    # the auto-derived LLM API endpoints and the host control plane.
    egress_allowlist: list[str] = field(default_factory=list)
    # Dedicated Docker bridge network used in restricted mode.
    egress_network: str = "sregym-agent-egress"


def extract_egress_hosts(env_vars: dict[str, str], extra: list[str] | None = None) -> list[str]:
    """Derive the set of hostnames a restricted agent must still reach.

    Pulls hostnames out of forwarded LLM provider endpoint variables (anything whose
    name ends in ``_API_BASE``/``_BASE_URL``/``_ENDPOINT`` or is a known base-URL var)
    so the agent can still talk to its model, then adds any explicitly configured
    ``extra`` hosts. Bare hostnames (no scheme) are accepted as-is. Returns a sorted,
    de-duplicated list. Pure — does no DNS resolution or I/O.
    """
    suffixes = ("_API_BASE", "_BASE_URL", "_API_BASE_URL", "_ENDPOINT", "_API_ENDPOINT")
    known = {"OPENAI_BASE_URL", "OPENAI_API_BASE", "ANTHROPIC_BASE_URL", "AZURE_API_BASE"}
    hosts: set[str] = set()
    for key, value in (env_vars or {}).items():
        if not value:
            continue
        if not (key.endswith(suffixes) or key in known):
            continue
        candidate = value.strip()
        parsed = urlparse(candidate if "://" in candidate else f"//{candidate}")
        host = parsed.hostname
        if host:
            hosts.add(host)
    for item in extra or []:
        item = item.strip()
        if not item:
            continue
        parsed = urlparse(item if "://" in item else f"//{item}")
        hosts.add(parsed.hostname or item)
    return sorted(hosts)


def build_egress_firewall_rules(
    subnet: str,
    gateway_ip: str,
    allow_ips: list[str],
    chain: str = "DOCKER-USER",
) -> list[list[str]]:
    """Build the ordered iptables rules that restrict a bridge subnet's egress.

    Enforced in the host's ``DOCKER-USER`` chain (traversed before Docker's own
    forwarding rules and outside the container's network namespace), so an agent
    that is root *inside* the container cannot remove them. The policy: allow return
    traffic and traffic to the host gateway (control plane) and each allowlisted LLM
    API IP, then drop everything else leaving the subnet. Pure — returns argv lists
    for the caller to execute; performs no I/O itself.
    """
    rules: list[list[str]] = [
        ["iptables", "-I", chain, "-s", subnet, "-m", "state", "--state", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
        ["iptables", "-I", chain, "-s", subnet, "-d", gateway_ip, "-j", "ACCEPT"],
    ]
    for ip in allow_ips:
        rules.append(["iptables", "-I", chain, "-s", subnet, "-d", ip, "-j", "ACCEPT"])
    # Final catch-all drop for this subnet (appended so the ACCEPTs above take precedence).
    rules.append(["iptables", "-A", chain, "-s", subnet, "-j", "DROP"])
    return rules


def rewrite_kubeconfig_server_host(text: str, new_host: str) -> str:
    """Rewrite localhost/127.0.0.1 ``server:`` hosts in a kubeconfig to ``new_host``.

    In restricted egress mode the agent is off the host network, so a kubeconfig that
    points the API server at 127.0.0.1/localhost is unreachable; it must instead target
    the host gateway (host.docker.internal). Only the host portion of ``server:`` URLs is
    changed; scheme, port and path are preserved. Pure text transform — easy to unit test.
    """
    out_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("server:"):
            indent = line[: len(line) - len(stripped)]
            url = stripped[len("server:") :].strip()
            parsed = urlparse(url)
            if parsed.hostname in ("127.0.0.1", "localhost"):
                netloc = new_host
                if parsed.port:
                    netloc = f"{new_host}:{parsed.port}"
                rebuilt = parsed._replace(netloc=netloc).geturl()
                out_lines.append(f"{indent}server: {rebuilt}")
                continue
        out_lines.append(line)
    return "\n".join(out_lines) + ("\n" if text.endswith("\n") else "")


class ContainerRunner:
    # Env vars forwarded from host to agent containers.
    # Sourced from litellm provider source code (llms/<provider>/).
    API_KEY_VARS = [
        # OpenAI
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "OPENAI_BASE_URL",
        # DeepSeek
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_API_BASE",
        # Anthropic
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_API_BASE",
        # Gemini / Google
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GEMINI_API_BASE",
        # Azure OpenAI
        "AZURE_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "AZURE_API_BASE",
        "AZURE_API_VERSION",
        "AZURE_AD_TOKEN",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
        "AZURE_TENANT_ID",
        "AZURE_USERNAME",
        "AZURE_PASSWORD",
        "AZURE_CERTIFICATE_PATH",
        "AZURE_CERTIFICATE_PASSWORD",
        "AZURE_CREDENTIAL",
        "AZURE_SCOPE",
        "AZURE_AUTHORITY_HOST",
        "AZURE_FEDERATED_TOKEN_FILE",
        # AWS Bedrock
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_REGION_NAME",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_SESSION_NAME",
        "AWS_PROFILE",
        "AWS_PROFILE_NAME",
        "AWS_ROLE_NAME",
        "AWS_ROLE_ARN",
        "AWS_WEB_IDENTITY_TOKEN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_STS_ENDPOINT",
        "AWS_EXTERNAL_ID",
        "AWS_BEDROCK_RUNTIME_ENDPOINT",
        "AWS_BEARER_TOKEN_BEDROCK",
        # WatsonX / IBM
        "WATSONX_API_KEY",
        "WATSONX_APIKEY",
        "WATSONX_API_BASE",
        "WATSONX_URL",
        "WATSONX_TOKEN",
        "WATSONX_PROJECT_ID",
        "WATSONX_REGION",
        "WATSONX_SPACE_ID",
        "WATSONX_DEPLOYMENT_SPACE_ID",
        "WATSONX_IAM_URL",
        "WATSONX_ZENAPIKEY",
        "WX_API_KEY",
        "WX_PROJECT_ID",
        "WX_URL",
        "WX_REGION",
        "WX_SPACE_ID",
        "WML_URL",
        # Vertex AI
        "VERTEXAI_PROJECT",
        "VERTEXAI_LOCATION",
        "VERTEX_LOCATION",
        "VERTEXAI_CREDENTIALS",
        "GOOGLE_APPLICATION_CREDENTIALS",
        # Moonshot
        "MOONSHOT_API_KEY",
        "MOONSHOT_API_BASE",
        # GLM
        "GLM_API_KEY",
        # Claude Code
        "CLAUDE_CODE_OAUTH_TOKEN",
        # SREGym internal
        "AGENT_MODEL_ID",
        "JUDGE_MODEL_ID",
        # Config vars
        "API_HOSTNAME",
        "API_PORT",
        "MCP_SERVER_PORT",
        "MCP_SERVER_URL",
        "EXPOSE_SERVER",
        "SESSION_CACHE_SIZE",
        "SESSION_TTL",
        "LLM_QUERY_MAX_RETRIES",
        "LLM_QUERY_INIT_RETRY_DELAY",
        "WAIT_FOR_POD_READY_TIMEOUT",
    ]

    def __init__(self, config: ContainerConfig | None = None):
        self.config = config or ContainerConfig()

    def _build_env_flags(self, extra_env: dict[str, str] | None = None) -> list[str]:
        flags = []
        env_vars = dict(self.config.env_vars)

        # Forward API keys from host (skip empty values to avoid overriding
        # other auth mechanisms like OAuth subscription tokens)
        for var in self.API_KEY_VARS:
            if var in os.environ and var not in env_vars and os.environ[var]:
                env_vars[var] = os.environ[var]

        if extra_env:
            env_vars.update(extra_env)

        # Decide whether the agent reaches host control-plane services via
        # host.docker.internal instead of localhost. This is required whenever the
        # container is NOT sharing the host network stack: on macOS (where
        # --network=host is a no-op) and in restricted egress mode (dedicated bridge).
        needs_host_gateway = (self.config.network_mode == "host" and platform.system() == "Darwin") or (
            self.config.egress_mode == EGRESS_RESTRICTED
        )
        if needs_host_gateway:
            env_vars["API_HOSTNAME"] = CONTROL_PLANE_HOST
            mcp_port = env_vars.get("MCP_SERVER_PORT", os.environ.get("MCP_SERVER_PORT", "9954"))
            env_vars["MCP_SERVER_URL"] = f"http://{CONTROL_PLANE_HOST}:{mcp_port}"

        for key, value in env_vars.items():
            flags.extend(["-e", f"{key}={value}"])
        return flags

    def _build_base_docker_args(self) -> list[str]:
        args = [
            "docker",
            "run",
            "--rm",
            f"--cpus={self.config.cpus}",
            f"--memory={self.config.memory}",
        ]

        # Restricted egress overrides the network mode: the agent is placed on a
        # dedicated bridge network (so its traffic is filterable in DOCKER-USER) and
        # reaches host control-plane services via host.docker.internal.
        if self.config.egress_mode == EGRESS_RESTRICTED:
            args.append(f"--network={self.config.egress_network}")
            args.append(f"--add-host={CONTROL_PLANE_HOST}:host-gateway")
        # Configure networking based on the network mode
        elif self.config.network_mode == "host":
            if platform.system() == "Darwin":
                # macOS: Don't use --network host (it's ignored), rely on host.docker.internal
                args.append("--add-host=host.docker.internal:host-gateway")
            else:
                # Linux: --network=host shares the host's network stack, so
                # localhost already reaches host services directly.
                args.append(f"--network={self.config.network_mode}")
        else:
            args.append(f"--network={self.config.network_mode}")

        # Mount kubeconfig (read-only). In restricted egress mode its API server host
        # is rewritten to the host gateway since the agent is off the host network.
        agent_kubeconfig = self._maybe_rewrite_kubeconfig(self.config.kubeconfig_path)
        if agent_kubeconfig and agent_kubeconfig.exists():
            args.extend(["-v", f"{agent_kubeconfig.resolve()}:/root/.kube/config:ro"])
            args.extend(["-e", "KUBECONFIG=/root/.kube/config"])

        # Mount the real (unproxied) kubeconfig so that workload oracles
        # running inside the container can bypass the filtering proxy.
        real_kubeconfig = Path(os.path.expanduser("~/.kube/config"))
        if real_kubeconfig.exists():
            real_mount = self._maybe_rewrite_kubeconfig(real_kubeconfig) or real_kubeconfig
            args.extend(["-v", f"{real_mount.resolve()}:/root/.kube/real-config:ro"])
            args.extend(["-e", "SREGYM_REAL_KUBECONFIG=/root/.kube/real-config"])

        # Mount AWS credentials directory (read-only) for Bedrock and other AWS services
        aws_dir = Path.home() / ".aws"
        if aws_dir.is_dir():
            args.extend(["-v", f"{aws_dir.resolve()}:/root/.aws:ro"])

        # Mount Codex credentials directory for subscription-based auth
        # (read-write so the CLI can update its model cache and telemetry)
        codex_dir = Path.home() / ".codex"
        if codex_dir.is_dir():
            args.extend(["-v", f"{codex_dir.resolve()}:/root/.codex"])

        # Mount workspace directory for agent output (logs, results, trajectories)
        if self.config.workspace_path:
            self.config.workspace_path.mkdir(parents=True, exist_ok=True)
            args.extend(["-v", f"{self.config.workspace_path.resolve()}:/workspace"])

        # Mount logs directory (for composite command tee output)
        if self.config.logs_path:
            self.config.logs_path.mkdir(parents=True, exist_ok=True)
            args.extend(["-v", f"{self.config.logs_path.resolve()}:/logs"])

        # Mount only the needed SREGym-applications subdirectories (read-only)
        if self.config.sregym_apps_path and self.config.sregym_app_subdirs:
            for subdir in self.config.sregym_app_subdirs:
                host_path = self.config.sregym_apps_path / subdir
                if host_path.exists():
                    args.extend(["-v", f"{host_path.resolve()}:/opt/sregym/SREGym-applications/{subdir}:ro"])

        # Mount source code for code-level bug investigation (read-write for code fixes)
        if self.config.source_code_path and self.config.source_code_path.exists():
            args.extend(["-v", f"{self.config.source_code_path.resolve()}:/opt/source:rw"])

        return args

    # ── Restricted egress (anti-cheat) ────────────────────────────────────────

    def _maybe_rewrite_kubeconfig(self, src: Path | None) -> Path | None:
        """In restricted egress mode, return a temp kubeconfig whose 127.0.0.1/localhost
        API server host is rewritten to the host gateway (reachable from the bridge via
        host.docker.internal). Returns ``src`` unchanged outside restricted mode or on error.
        """
        if not src or not src.exists() or self.config.egress_mode != EGRESS_RESTRICTED:
            return src
        try:
            rewritten = rewrite_kubeconfig_server_host(src.read_text(), CONTROL_PLANE_HOST)
            tmp = Path(tempfile.gettempdir()) / f"sregym-agent-kubeconfig-{uuid.uuid4().hex[:8]}.yaml"
            tmp.write_text(rewritten)
            return tmp
        except Exception as e:
            logger.warning(f"[egress] kubeconfig rewrite failed, using original: {e}")
            return src

    def prepare_egress(self) -> None:
        """Best-effort setup for restricted egress; no-op in open mode.

        Ensures the dedicated bridge network exists and, when explicitly enabled via
        ``SREGYM_AGENT_EGRESS_APPLY_FIREWALL=1``, locks its egress down to the host
        control plane plus the LLM API endpoints using host ``DOCKER-USER`` rules (which
        the in-container agent, even as root, cannot remove). All failures are logged
        rather than raised so a host misconfiguration never silently wedges the agent —
        but note that without the firewall the bridge still has open egress.
        """
        if self.config.egress_mode != EGRESS_RESTRICTED:
            return
        subnet, gateway = self._ensure_egress_network()
        if not subnet or not gateway:
            logger.warning("[egress] could not determine bridge subnet/gateway; egress NOT restricted")
            return
        if os.environ.get("SREGYM_AGENT_EGRESS_APPLY_FIREWALL", "") == "1":
            self._apply_egress_firewall(subnet, gateway)
        else:
            logger.warning(
                "[egress] restricted network '%s' ready (subnet %s) but host firewall NOT applied; "
                "set SREGYM_AGENT_EGRESS_APPLY_FIREWALL=1 to enforce. Egress is still open.",
                self.config.egress_network,
                subnet,
            )

    def _ensure_egress_network(self) -> tuple[str | None, str | None]:
        """Create the dedicated bridge network if missing; return its (subnet, gateway)."""
        name = self.config.egress_network
        inspect = subprocess.run(["docker", "network", "inspect", name], capture_output=True, text=True)
        if inspect.returncode != 0:
            subprocess.run(
                ["docker", "network", "create", "--driver", "bridge", name],
                capture_output=True,
                text=True,
            )
            inspect = subprocess.run(["docker", "network", "inspect", name], capture_output=True, text=True)
        if inspect.returncode != 0:
            return None, None
        try:
            cfg = json.loads(inspect.stdout)[0]["IPAM"]["Config"][0]
            return cfg.get("Subnet"), cfg.get("Gateway")
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
            logger.warning(f"[egress] failed to parse network config for '{name}': {e}")
            return None, None

    def _apply_egress_firewall(self, subnet: str, gateway: str) -> None:
        """Resolve the allowlist to IPs and install the DOCKER-USER egress rules."""
        forwarded = {k: os.environ.get(k, "") for k in self.API_KEY_VARS}
        forwarded.update(self.config.env_vars)
        hosts = extract_egress_hosts(forwarded, self.config.egress_allowlist)
        allow_ips = self._resolve_hosts(hosts)
        logger.info("[egress] allowlisting %d host(s) → %d IP(s): %s", len(hosts), len(allow_ips), hosts)
        for rule in build_egress_firewall_rules(subnet, gateway, allow_ips):
            r = subprocess.run(rule, capture_output=True, text=True)
            if r.returncode != 0:
                logger.warning("[egress] iptables rule failed (%s): %s", " ".join(rule), r.stderr.strip())

    @staticmethod
    def _resolve_hosts(hosts: list[str]) -> list[str]:
        """Resolve hostnames to a sorted, de-duplicated list of IP addresses."""
        ips: set[str] = set()
        for h in hosts:
            try:
                for info in socket.getaddrinfo(h, None):
                    ips.add(info[4][0])
            except OSError:
                logger.warning("[egress] could not resolve %s; it will be unreachable", h)
        return sorted(ips)

    def build_docker_command(self, exec_input: ExecInput) -> list[str]:
        cmd = self._build_base_docker_args()
        suffix = uuid.uuid4().hex[:8]
        if exec_input.label:
            container_name = f"sregym-{exec_input.label}-{suffix}"
            cmd.extend(["--name", container_name])
            exec_input.container_name = container_name
        cmd.extend(self._build_env_flags(exec_input.env))
        cmd.append(self.config.image)
        cmd.append(exec_input.command)
        return cmd

    def build_composite_command(
        self,
        install_script: str | None,
        agent_version: str | None,
        driver_command: str,
    ) -> str:
        parts = []

        if install_script:
            version_env = f'AGENT_VERSION="{agent_version}" ' if agent_version else ""
            parts.append(
                f"{version_env}/opt/sregym/install-scripts/{install_script} 2>&1 "
                f"| tee /logs/install.log; INSTALL_RC=${{PIPESTATUS[0]}}; "
                f'echo "$INSTALL_RC" > /logs/install.rc; '
                f'[ "$INSTALL_RC" -eq 0 ] || exit "$INSTALL_RC"'
            )

        parts.append(
            f"{driver_command} 2>&1 "
            f"| tee /logs/driver.log; DRIVER_RC=${{PIPESTATUS[0]}}; "
            f'echo "$DRIVER_RC" > /logs/driver.rc; '
            f'exit "$DRIVER_RC"'
        )

        return " && ".join(parts)

    def run_sync(self, exec_input: ExecInput) -> subprocess.CompletedProcess:
        """Run a short-lived command in a container and wait for it to finish.

        Unlike run_async, this blocks until the container exits and returns the
        CompletedProcess with captured stdout/stderr.  Useful for pre-flight
        checks (e.g. model validation) that must complete before the main
        agent container is launched.
        """
        cmd = self.build_docker_command(exec_input)

        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=exec_input.timeout,
            )
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            if exec_input.container_name:
                ContainerRunner.stop_container(exec_input.container_name, timeout=5)
            raise

    def run_async(self, exec_input: ExecInput) -> subprocess.Popen:
        """Start an agent in a container asynchronously. Returns Popen handle."""
        cmd = self.build_docker_command(exec_input)
        logger.info(f"Starting containerized agent [{exec_input.label}]: {exec_input.command[:80]}...")

        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

    def ensure_image_exists(self) -> None:
        """Check if the container image exists locally; build it if not."""
        image = self.config.image
        result = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
        )
        if result.returncode == 0:
            return

        logger.info(f"🐳 Container image '{image}' not found. Building automatically...")
        self.build_image()

    def build_image(self) -> None:
        """Build (or rebuild) the container image using docker/agents/build.sh."""
        image = self.config.image
        logger.info(f"🐳 Building container image '{image}'...")

        repo_root = Path(__file__).resolve().parent.parent.parent
        build_script = repo_root / "docker" / "agents" / "build.sh"

        if not build_script.exists():
            raise FileNotFoundError(
                f"Build script not found at {build_script}. Cannot auto-build container image '{image}'."
            )

        build_script.chmod(build_script.stat().st_mode | 0o755)
        result = subprocess.run(
            [str(build_script)],
            cwd=str(repo_root),
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to build container image '{image}'. Check the build output above for errors.")
        logger.info(f"✅ Container image '{image}' built successfully.")

    @staticmethod
    def stop_container(container_name: str, timeout: int = 10) -> None:
        """Stop a running container by name. Used for cleanup."""
        try:
            subprocess.run(
                ["docker", "stop", "-t", str(timeout), container_name],
                capture_output=True,
                timeout=timeout + 5,
            )
        except Exception:
            # Force remove if stop fails
            try:
                subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    capture_output=True,
                    timeout=5,
                )
            except Exception:
                # Force remove if stop fails
                with contextlib.suppress(Exception):
                    subprocess.run(
                        ["docker", "rm", "-f", container_name],
                        capture_output=True,
                        timeout=5,
                    )
