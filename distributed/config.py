from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class NodeConfig:
    host: str
    user: str
    ssh_key: str | None = None
    sregym_path: str = "~/sregym/SREGym"
    label: str = ""
    port: int = 22

    def __post_init__(self):
        if not self.label:
            self.label = self.host.split(".")[0]


def load_nodes(path: str | Path) -> list[NodeConfig]:
    """Load node configs from a distributed_hosts.yaml or ansible inventory.yml."""
    path = Path(path)
    with open(path) as f:
        data = yaml.safe_load(f)

    # Detect format: distributed_hosts.yaml has top-level "nodes" key
    if "nodes" in data:
        return _parse_hosts_yaml(data)

    # Ansible inventory format: all.children.{control_nodes,worker_nodes}.hosts
    if "all" in data:
        return _parse_ansible_inventory(data)

    raise ValueError(f"Unrecognized config format in {path}. Expected 'nodes' or 'all' top-level key.")


def _parse_hosts_yaml(data: dict) -> list[NodeConfig]:
    nodes = []
    for entry in data["nodes"]:
        nodes.append(
            NodeConfig(
                host=entry["host"],
                user=entry["user"],
                ssh_key=entry.get("ssh_key"),
                sregym_path=entry.get("sregym_path", "~/sregym/SREGym"),
                label=entry.get("label", ""),
                port=entry.get("port", 22),
            )
        )
    return nodes


def _parse_ansible_inventory(data: dict) -> list[NodeConfig]:
    """Parse ansible inventory.yml format, treating all hosts as runner nodes."""
    nodes = []
    all_section = data.get("all", {})
    global_vars = all_section.get("vars", {})
    children = all_section.get("children", {})

    for _group_name, group in children.items():
        hosts = group.get("hosts", {})
        for host_name, host_vars in hosts.items():
            if host_vars is None:
                host_vars = {}
            # Resolve ansible_user with variable substitution
            user = host_vars.get("ansible_user", "")
            for var_name, var_value in global_vars.items():
                user = user.replace(f"{{{{ {var_name} }}}}", str(var_value))

            nodes.append(
                NodeConfig(
                    host=host_vars.get("ansible_host", host_name),
                    user=user,
                    label=host_name,
                )
            )
    return nodes
