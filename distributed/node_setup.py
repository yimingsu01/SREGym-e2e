"""Node setup script for the distributed runner.

Runs directly on a CloudLab node (invoked via asyncssh) to install
dependencies and prepare the environment for SREGym.

Usage (on the remote node):
    python3 node_setup.py --install --sregym-path /users/lilygn/SREGym
    python3 node_setup.py --setup-env --sregym-path /users/lilygn/SREGym
    python3 node_setup.py --create-cluster --sregym-path /users/lilygn/SREGym
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

TIMEOUT = 1800  # 30 minutes

ENV = {
    **os.environ,
    "CI": "1",
    "NONINTERACTIVE": "1",
    "DEBIAN_FRONTEND": "noninteractive",
    "SUDO_ASKPASS": "/bin/false",
}


def _run(cmd: str, desc: str, timeout: int = TIMEOUT) -> None:
    """Run a shell command locally, raising on failure."""
    print(f"\n==> {desc}")
    print(f"  $ {cmd}")
    subprocess.run(
        cmd,
        shell=True,
        executable="/bin/bash",
        env=ENV,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        check=True,
    )
    print(f"  OK: {desc}")


def _brew_shellenv() -> str:
    """Return the shell snippet to activate Homebrew."""
    if Path("/home/linuxbrew/.linuxbrew/bin/brew").exists():
        return 'eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"'
    return 'eval "$(brew shellenv)"'


def _has_brew() -> bool:
    return (
        Path("/home/linuxbrew/.linuxbrew/bin/brew").exists()
        or shutil.which("brew") is not None
    )


# ── Installation steps ──────────────────────────────────────────────


def install_brew() -> None:
    if _has_brew():
        print("Homebrew already installed, skipping.")
        return
    _run(
        'NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL '
        'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"',
        "Install Homebrew",
    )
    # Add to bashrc so future shells pick it up
    bashrc = Path.home() / ".bashrc"
    shellenv_line = 'eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"'
    if bashrc.exists() and shellenv_line not in bashrc.read_text():
        with open(bashrc, "a") as f:
            f.write(f"\n{shellenv_line}\n")
    _run(
        f'{_brew_shellenv()}; sudo apt-get install -y build-essential && brew install gcc',
        "Install build-essential and gcc",
    )


def install_go() -> None:
    arch = platform.machine()
    if arch == "x86_64":
        tarball = "go1.24.1.linux-amd64.tar.gz"
    elif arch in ("aarch64", "arm64"):
        tarball = "go1.24.1.linux-arm64.tar.gz"
    else:
        tarball = f"go1.24.1.linux-{arch}.tar.gz"

    _run(
        f"wget -q https://go.dev/dl/{tarball} -O /tmp/{tarball} && "
        f"sudo rm -rf /usr/local/go && "
        f"sudo tar -C /usr/local -xzf /tmp/{tarball} && "
        f"rm -f /tmp/{tarball}",
        "Install Go",
    )
    # Ensure PATH has go
    bashrc = Path.home() / ".bashrc"
    for line in ['export PATH=$PATH:/usr/local/go/bin', 'export PATH=$PATH:$(go env GOPATH)/bin']:
        if bashrc.exists() and line not in bashrc.read_text():
            with open(bashrc, "a") as f:
                f.write(f"\n{line}\n")


def install_docker() -> None:
    if shutil.which("docker"):
        print("Docker already installed, skipping.")
        return
    _run(
        "sudo apt-get update -qq && "
        "sudo apt-get install -y -qq ca-certificates curl && "
        "sudo install -m 0755 -d /etc/apt/keyrings && "
        "sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg "
        "  -o /etc/apt/keyrings/docker.asc && "
        "sudo chmod a+r /etc/apt/keyrings/docker.asc",
        "Add Docker GPG key",
    )
    _run(
        'echo "deb [arch=$(dpkg --print-architecture) '
        'signed-by=/etc/apt/keyrings/docker.asc] '
        "https://download.docker.com/linux/ubuntu "
        '$(. /etc/os-release && echo ${UBUNTU_CODENAME:-$VERSION_CODENAME}) stable" '
        "| sudo tee /etc/apt/sources.list.d/docker.list > /dev/null && "
        "sudo apt-get update -qq && "
        "sudo apt-get install -y -qq "
        "docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin",
        "Install Docker",
    )
    _run(
        "sudo groupadd docker 2>/dev/null || true && "
        "sudo usermod -aG docker $USER && "
        "sudo chown $USER /var/run/docker.sock 2>/dev/null || true",
        "Configure Docker group",
    )


def install_kind() -> None:
    shellenv = _brew_shellenv()
    if _has_brew():
        _run(f"{shellenv}; brew install kind || true", "Install kind (brew)")
    else:
        arch = platform.machine()
        if arch == "x86_64":
            arch = "amd64"
        elif arch in ("aarch64", "arm64"):
            arch = "arm64"
        _run(
            f'curl -fsSL "https://kind.sigs.k8s.io/dl/v0.23.0/kind-linux-{arch}" -o /tmp/kind && '
            "chmod +x /tmp/kind && sudo mv /tmp/kind /usr/local/bin/kind",
            "Install kind (binary)",
        )


def install_python() -> None:
    shellenv = _brew_shellenv()
    _run(f"{shellenv}; brew install python@3.12 || true", "Install Python 3.12")


def install_git() -> None:
    shellenv = _brew_shellenv()
    _run(f"{shellenv}; brew install git || true", "Install git")


def install_kubectl() -> None:
    shellenv = _brew_shellenv()
    _run(f"{shellenv}; brew install kubectl helm || true", "Install kubectl & helm")


def do_install() -> None:
    """Run all installation steps."""
    install_brew()
    install_go()
    install_docker()
    install_kind()
    install_python()
    install_git()
    install_kubectl()
    print("\n=== All installations complete ===")


# ── Environment setup ───────────────────────────────────────────────


def do_setup_env(sregym_path: str) -> None:
    """Set up uv, venv, and sync deps."""
    shellenv = _brew_shellenv()

    # Install uv if missing
    _run(
        f"{shellenv}; command -v uv >/dev/null 2>&1 || brew install uv",
        "Ensure uv is installed",
    )

    # Create venv + sync
    _run(
        f"cd {sregym_path} && "
        f"{shellenv} && "
        'UV_VENV_CLEAR=1 uv venv -p "$(command -v python3.12 || command -v python3)" && '
        "source .venv/bin/activate && "
        "uv sync",
        "Create venv and sync dependencies",
    )
    print("\n=== Environment setup complete ===")


# ── Cluster creation ────────────────────────────────────────────────


def do_create_cluster(sregym_path: str) -> None:
    """Create a kind cluster using the repo's config, unless a cluster already exists."""
    shellenv = _brew_shellenv()

    # Check if ANY healthy k8s cluster already exists (bare-metal kubeadm, kind, etc.)
    try:
        result = subprocess.run(
            f"{shellenv}; kubectl cluster-info 2>&1",
            shell=True, executable="/bin/bash", capture_output=True, text=True,
            timeout=15,
        )
        if result.returncode == 0 and "running" in (result.stdout or "").lower():
            print("Kubernetes cluster already running, skipping kind creation.")
            return
    except Exception:
        pass

    # Also check kind specifically
    try:
        result = subprocess.run(
            f"{shellenv}; kubectl cluster-info --context kind-kind 2>&1",
            shell=True, executable="/bin/bash", capture_output=True, text=True,
            timeout=15,
        )
        if result.returncode == 0 and "running" in (result.stdout or "").lower():
            print("Kind cluster already running and healthy, skipping creation.")
            return
        else:
            print("No healthy cluster found, will create kind cluster...")
    except Exception:
        pass

    # Delete any stale cluster and reclaim disk space
    subprocess.run(
        f"{shellenv}; kind delete cluster 2>/dev/null",
        shell=True, executable="/bin/bash",
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print("Cleaning up old Docker resources...")
    subprocess.run(
        "docker system prune -af --volumes 2>/dev/null; docker builder prune -af 2>/dev/null",
        shell=True, executable="/bin/bash",
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    kind_dir = Path(sregym_path) / "kind"
    kind_dir.mkdir(parents=True, exist_ok=True)

    # Create lightweight configs if they don't exist (1 control-plane + 1 worker)
    for arch, image in [("x86", "aiopslab-kind-x86"), ("arm", "aiopslab-kind-arm")]:
        light_path = kind_dir / f"kind-config-{arch}-light.yaml"
        if not light_path.exists():
            light_path.write_text(
                "kind: Cluster\n"
                "apiVersion: kind.x-k8s.io/v1alpha4\n"
                "nodes:\n"
                "  - role: control-plane\n"
                f"    image: jacksonarthurclark/{image}:latest\n"
                "    extraMounts:\n"
                "      - hostPath: /run/udev\n"
                "        containerPath: /run/udev\n"
                "  - role: worker\n"
                f"    image: jacksonarthurclark/{image}:latest\n"
                "    extraMounts:\n"
                "      - hostPath: /run/udev\n"
                "        containerPath: /run/udev\n"
            )
            print(f"Created lightweight kind config at {light_path}")

    config = None
    # Use light config (1 control-plane + 1 worker) to save disk space
    # 157GB RAM + 40 cores is plenty — disk (63GB) is the bottleneck
    for name in ["kind-config-x86-light.yaml", "kind-config-arm-light.yaml", "kind-config-x86.yaml", "kind-config-arm.yaml"]:
        candidate = kind_dir / name
        if candidate.is_file():
            config = str(candidate)
            break
    if config is None:
        for p in sorted(kind_dir.glob("*.yaml")):
            if p.is_file():
                config = str(p)
                break

    config_flag = f"--config {config}" if config else ""
    _run(
        f"{shellenv}; kind create cluster {config_flag}",
        "Create kind cluster",
    )
    # Pre-pull images into the kind cluster to avoid timeout during first deploy
    _prepull_images()
    print("\n=== Cluster created ===")


# Images used by SREGym applications
_IMAGES_TO_PREPULL = [
    "deathstarbench/hotel-reservation:latest",
    "mongo:latest",
    "memcached:latest",
    "hashicorp/consul:latest",
    "jaegertracing/all-in-one:latest",
    "yinfangchen/hotel-reservation:latest",
    "jackcuii/hotel-reservation:latest",
    "yinfangchen/geo:app3",
    "grafana/loki:latest",
    "prom/prometheus:latest",
    "otel/opentelemetry-collector:latest",
]


def _prepull_images() -> None:
    """Pull images, load into kind, then remove from docker to save disk."""
    shellenv = _brew_shellenv()
    print("\n==> Pre-pulling images into kind cluster (one at a time to save disk)...")

    for img in _IMAGES_TO_PREPULL:
        print(f"  {img}: pulling...")
        subprocess.run(
            f"docker pull {img}",
            shell=True, executable="/bin/bash",
            env=ENV, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print(f"  {img}: loading into kind...")
        subprocess.run(
            f"{shellenv}; kind load docker-image {img} 2>/dev/null",
            shell=True, executable="/bin/bash",
            env=ENV, stdin=subprocess.DEVNULL,
            timeout=300,
        )
        # Remove from docker host to free disk (it's now inside kind nodes)
        subprocess.run(
            f"docker rmi {img} 2>/dev/null",
            shell=True, executable="/bin/bash",
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    # Clean up any dangling images/build cache
    subprocess.run(
        "docker builder prune -af 2>/dev/null",
        shell=True, executable="/bin/bash",
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print("  Images pre-loaded into kind cluster.")


# ── Patch main.py ───────────────────────────────────────────────────


def do_patch_main(sregym_path: str) -> None:
    """Patch main.py so deploy failures skip instead of crashing the run."""
    import re

    main_py = Path(sregym_path) / "main.py"
    if not main_py.exists():
        print(f"main.py not found at {main_py}, skipping patch")
        return

    text = main_py.read_text()
    # Replace: raise RuntimeError(f"All {max_deploy_retries} deploy attempts failed ...")
    # With:    continue  (skip to next problem)
    patched = re.sub(
        r'raise RuntimeError\(f"All \{max_deploy_retries\} deploy attempts failed.*',
        "continue  # patched: skip failed deploys instead of crashing",
        text,
    )
    if patched != text:
        main_py.write_text(patched)
        print("Patched main.py: deploy failures now skip instead of crash")
    else:
        print("main.py: no patch needed (already patched or pattern not found)")


# ── CLI ─────────────────────────────────────────────────────────────


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="SREGym node setup (runs locally on node)")
    parser.add_argument("--install", action="store_true", help="Install all dependencies")
    parser.add_argument("--setup-env", action="store_true", help="Set up Python venv and uv sync")
    parser.add_argument("--create-cluster", action="store_true", help="Create kind cluster")
    parser.add_argument("--patch-main", action="store_true", help="Patch main.py to skip deploy failures")
    parser.add_argument("--all", action="store_true", help="Run install + setup-env + create-cluster")
    parser.add_argument("--sregym-path", type=str, default="~/SREGym", help="Path to SREGym on this node")
    args = parser.parse_args()

    sregym = str(Path(args.sregym_path).expanduser())

    if args.all or args.install:
        do_install()
    if args.all or args.setup_env:
        do_setup_env(sregym)
    if args.all or args.create_cluster:
        do_create_cluster(sregym)
    if args.patch_main:
        do_patch_main(sregym)

    if not any([args.install, args.setup_env, args.create_cluster, args.all, args.patch_main]):
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
