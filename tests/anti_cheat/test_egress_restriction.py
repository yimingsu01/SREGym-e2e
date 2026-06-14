"""Unit tests for restricted agent egress (anti-cheat).

These cover the pure decision logic — which hosts stay reachable, the iptables rule
ordering, the kubeconfig host rewrite — plus the docker-argument/env wiring for the
restricted vs open (default) modes. No Docker daemon or iptables is exercised.
"""

from sregym.service.container_runner import (
    EGRESS_OPEN,
    EGRESS_RESTRICTED,
    ContainerConfig,
    ContainerRunner,
    build_egress_firewall_rules,
    extract_egress_hosts,
    rewrite_kubeconfig_server_host,
)


def _env_flag_dict(flags):
    out = {}
    for i in range(len(flags) - 1):
        if flags[i] == "-e" and "=" in flags[i + 1]:
            k, _, v = flags[i + 1].partition("=")
            out[k] = v
    return out


# ── extract_egress_hosts ──────────────────────────────────────────────────────


def test_extract_hosts_from_base_urls():
    env = {
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
        "SOME_API_BASE": "https://gateway.example.com:8443/llm",
        "OPENAI_API_KEY": "sk-secret",  # not an endpoint var → ignored
        "PATH": "/usr/bin",  # unrelated → ignored
    }
    hosts = extract_egress_hosts(env)
    assert hosts == ["api.anthropic.com", "api.openai.com", "gateway.example.com"]


def test_extract_hosts_includes_extra_and_bare():
    hosts = extract_egress_hosts({}, extra=["proxy.internal", "https://extra.example.com"])
    assert hosts == ["extra.example.com", "proxy.internal"]


def test_extract_hosts_dedupes():
    env = {"OPENAI_BASE_URL": "https://api.openai.com/v1", "OPENAI_API_BASE": "https://api.openai.com/v2"}
    assert extract_egress_hosts(env) == ["api.openai.com"]


def test_extract_hosts_ignores_empty():
    assert extract_egress_hosts({"OPENAI_BASE_URL": ""}, extra=["", "  "]) == []


# ── build_egress_firewall_rules ───────────────────────────────────────────────


def test_firewall_rules_order_and_content():
    rules = build_egress_firewall_rules("172.30.0.0/16", "172.30.0.1", ["1.2.3.4", "5.6.7.8"])
    # Established/related first, then gateway, then each allow IP, then a final DROP.
    assert rules[0][-1] == "ACCEPT" and "ESTABLISHED,RELATED" in rules[0]
    assert rules[1] == ["iptables", "-I", "DOCKER-USER", "-s", "172.30.0.0/16", "-d", "172.30.0.1", "-j", "ACCEPT"]
    assert ["iptables", "-I", "DOCKER-USER", "-s", "172.30.0.0/16", "-d", "1.2.3.4", "-j", "ACCEPT"] in rules
    assert ["iptables", "-I", "DOCKER-USER", "-s", "172.30.0.0/16", "-d", "5.6.7.8", "-j", "ACCEPT"] in rules
    assert rules[-1] == ["iptables", "-A", "DOCKER-USER", "-s", "172.30.0.0/16", "-j", "DROP"]
    # Every rule scopes to the subnet and targets the configured chain.
    assert all("172.30.0.0/16" in r and "DOCKER-USER" in r for r in rules)


def test_firewall_rules_custom_chain():
    rules = build_egress_firewall_rules("10.0.0.0/24", "10.0.0.1", [], chain="FORWARD")
    assert all("FORWARD" in r for r in rules)
    assert rules[-1][-1] == "DROP"


# ── rewrite_kubeconfig_server_host ────────────────────────────────────────────


def test_rewrite_kubeconfig_localhost_and_loopback():
    text = (
        "apiVersion: v1\n"
        "clusters:\n"
        "- cluster:\n"
        "    server: http://127.0.0.1:9955\n"
        "  name: agent\n"
        "- cluster:\n"
        "    server: https://localhost:6443\n"
        "  name: real\n"
    )
    out = rewrite_kubeconfig_server_host(text, "host.docker.internal")
    assert "server: http://host.docker.internal:9955" in out
    assert "server: https://host.docker.internal:6443" in out
    assert "127.0.0.1" not in out and "localhost" not in out


def test_rewrite_kubeconfig_leaves_remote_untouched():
    text = "    server: https://10.20.30.40:6443\n"
    assert rewrite_kubeconfig_server_host(text, "host.docker.internal") == text


# ── docker arg / env wiring ───────────────────────────────────────────────────


def test_open_mode_does_not_use_egress_network():
    runner = ContainerRunner(ContainerConfig(egress_mode=EGRESS_OPEN))
    args = runner._build_base_docker_args()
    joined = " ".join(args)
    assert "sregym-agent-egress" not in joined


def test_restricted_mode_uses_dedicated_network_and_gateway():
    runner = ContainerRunner(ContainerConfig(egress_mode=EGRESS_RESTRICTED))
    args = runner._build_base_docker_args()
    assert "--network=sregym-agent-egress" in args
    assert "--add-host=host.docker.internal:host-gateway" in args
    # Restricted mode must not also request host networking.
    assert "--network=host" not in args


def test_restricted_mode_rewires_control_plane_env():
    runner = ContainerRunner(ContainerConfig(egress_mode=EGRESS_RESTRICTED, env_vars={"MCP_SERVER_PORT": "9954"}))
    env = _env_flag_dict(runner._build_env_flags())
    assert env["API_HOSTNAME"] == "host.docker.internal"
    assert env["MCP_SERVER_URL"] == "http://host.docker.internal:9954"


def test_prepare_egress_noop_in_open_mode():
    # Open mode must never touch Docker networks or the firewall.
    runner = ContainerRunner(ContainerConfig(egress_mode=EGRESS_OPEN))
    runner.prepare_egress()  # should return immediately without raising
