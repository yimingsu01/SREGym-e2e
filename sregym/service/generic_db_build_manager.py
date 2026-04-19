"""Generic database build manager driven by a DBBuildSpec.

Mirrors CassandraBuildManager but works for any database type.

Two entry points:
  build_from_directory()   — source tree already contains the bug (cloned at
                             the buggy commit via GitHubIssueParser).  Used by
                             auto-generated problems.
  build_with_patches()     — apply hand-crafted patch files first, then build.
                             Used by hand-crafted problems.

Both return an image tag that is:
  - present in the local Docker daemon
  - loaded into the active cluster (kind / SSH / DaemonSet fallback)
"""

import hashlib
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from sregym.service.db_build_spec import DBBuildSpec

logger = logging.getLogger(__name__)

_DOCKERFILE_VERSION = "v1"


class GenericDBBuildManager:
    def __init__(self, spec: DBBuildSpec, source_path: Path, version: str):
        self.spec = spec
        self.source_path = Path(source_path)
        self.version = version

    # ── Public API ────────────────────────────────────────────────────────────

    def build_from_directory(self) -> str:
        """Build an image from the current source tree (already in buggy state).

        Image tag is derived from a hash of the full source tree so that
        identical source produces the same cached image.
        """
        source_hash = self._hash_dir(self.source_path)
        image_tag = self._make_tag(source_hash)

        if self._image_exists_locally(image_tag):
            logger.info(f"[GenericBuildMgr] Cached image {image_tag} — skipping build")
            self._load_into_cluster(image_tag)
            return image_tag

        logger.info(f"[GenericBuildMgr] Building from source tree: {image_tag}")
        self._build_artifact()
        self._build_docker_image(image_tag)
        self._load_into_cluster(image_tag)
        return image_tag

    def build_with_patches(self, patch_dir: Path) -> str:
        """Apply patch files, build, package.  Returns image name:tag."""
        patch_dir = Path(patch_dir)
        if not patch_dir.exists():
            raise FileNotFoundError(f"Patch directory not found: {patch_dir}")

        patch_hash = self._hash_dir(patch_dir)
        image_tag = self._make_tag(patch_hash)

        if self._image_exists_locally(image_tag):
            logger.info(f"[GenericBuildMgr] Cached image {image_tag} — skipping build")
            self._load_into_cluster(image_tag)
            return image_tag

        logger.info(f"[GenericBuildMgr] Building with patches: {image_tag}")
        self._apply_patches(patch_dir)
        self._build_artifact()
        self._build_docker_image(image_tag)
        self._load_into_cluster(image_tag)
        return image_tag

    # ── Build steps ───────────────────────────────────────────────────────────

    def _apply_patches(self, patch_dir: Path):
        patched = []
        for patch_file in sorted(patch_dir.rglob("*")):
            if patch_file.is_file():
                relative = patch_file.relative_to(patch_dir)
                dest = self.source_path / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(patch_file, dest)
                patched.append(str(relative))
        if patched:
            logger.info(f"[GenericBuildMgr] Patched {len(patched)} file(s)")
        else:
            logger.warning("[GenericBuildMgr] No files found in patch directory")

    def _build_artifact(self):
        """Run spec.build_cmd inside spec.build_image with the source tree mounted."""
        logger.info(
            f"[GenericBuildMgr] Building {self.spec.name} inside {self.spec.build_image} "
            f"— command: {self.spec.build_cmd}"
        )
        cmd = (
            f"docker run --rm "
            f"--network=host "
            f"-v {self.source_path}:{self.source_path} "
            f"-w {self.source_path} "
            f"{self.spec.build_image} "
            f"bash -c '{self.spec.build_cmd}'"
        )
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            out = result.stdout[-3000:] if result.stdout else ""
            err = result.stderr[-3000:] if result.stderr else ""
            raise RuntimeError(
                f"Build failed for {self.spec.name}:\n"
                f"--- stdout ---\n{out}\n--- stderr ---\n{err}"
            )
        logger.info(f"[GenericBuildMgr] Build succeeded")

    def _build_docker_image(self, image_tag: str):
        _exclude = ("-tests.jar", "-sources.jar", "-javadoc.jar", "-test-sources.jar")
        artifacts = [
            a for a in sorted(self.source_path.glob(self.spec.artifact_glob))
            if not any(a.name.endswith(s) for s in _exclude)
        ]
        if not artifacts:
            raise FileNotFoundError(
                f"No artifact matched '{self.spec.artifact_glob}' under {self.source_path}. "
                f"Check that the build succeeded."
            )
        artifact = artifacts[0]
        if len(artifacts) > 1:
            logger.warning(f"[GenericBuildMgr] Multiple artifacts matched, using {artifact.name}")

        base_image = self.spec.resolved_base_image(self.version)
        artifact_dest = self.spec.resolved_artifact_dest(self.version)
        build_ctx = Path(tempfile.mkdtemp(prefix=f"sregym-{self.spec.name}-docker-"))
        try:
            dest_filename = artifact.name
            shutil.copy2(artifact, build_ctx / dest_filename)
            dockerfile = "\n".join([
                f"FROM {base_image}",
                "USER root",
                f"COPY {dest_filename} {artifact_dest}",
            ])
            (build_ctx / "Dockerfile").write_text(dockerfile)

            platform = self._cluster_platform()
            logger.info(f"[GenericBuildMgr] docker buildx build {image_tag} for {platform}")
            result = subprocess.run(
                f"docker buildx build --platform {platform} --load -t {image_tag} .",
                cwd=build_ctx,
                shell=True,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"docker buildx build failed:\n"
                    f"stdout: {result.stdout}\nstderr: {result.stderr}"
                )
            logger.info(f"[GenericBuildMgr] Image built: {image_tag}")
        finally:
            shutil.rmtree(build_ctx, ignore_errors=True)

    # ── Cluster image distribution ────────────────────────────────────────────
    # Same three-strategy fallback as CassandraBuildManager:
    #   1. kind load docker-image
    #   2. SSH push (docker save | gzip | ssh node docker load)
    #   3. Privileged DaemonSet via kubectl cp + docker load

    def _load_into_cluster(self, image_tag: str):
        if subprocess.run(
            f"kind load docker-image {image_tag}",
            shell=True, capture_output=True, text=True,
        ).returncode == 0:
            logger.info(f"[GenericBuildMgr] Loaded {image_tag} into kind cluster")
            return

        node_ips = self._get_cluster_node_ips()
        if node_ips:
            ssh_user = self._ssh_user()
            ssh_opts = self._ssh_opts()
            failed = []
            for ip in node_ips:
                cmd = (
                    f"docker save {image_tag} | gzip | "
                    f"ssh {ssh_opts} {ssh_user}@{ip} 'sudo docker load || docker load'"
                )
                r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                if r.returncode == 0:
                    logger.info(f"[GenericBuildMgr] Image loaded on {ip}")
                else:
                    logger.warning(f"[GenericBuildMgr] SSH load failed on {ip}: {r.stderr.strip()}")
                    failed.append(ip)
            if not failed:
                return

        self._load_via_daemonset(image_tag)

    @staticmethod
    def _get_cluster_node_ips() -> list[str]:
        result = subprocess.run(
            "kubectl get nodes -o jsonpath='{.items[*].status.addresses[?(@.type==\"InternalIP\")].address}'",
            shell=True, capture_output=True, text=True,
        )
        if result.returncode != 0:
            return []
        return [ip for ip in result.stdout.strip().strip("'").split() if ip]

    @staticmethod
    def _ssh_user() -> str:
        import getpass, os
        return os.environ.get("SREGYM_CLUSTER_SSH_USER", "") or getpass.getuser()

    @staticmethod
    def _ssh_opts() -> str:
        import os
        opts = "-o StrictHostKeyChecking=no -o ConnectTimeout=15 -o BatchMode=yes"
        if key := os.environ.get("SREGYM_CLUSTER_SSH_KEY", ""):
            opts += f" -i {key}"
        return opts

    def _load_via_daemonset(self, image_tag: str):
        import uuid
        ns = "default"
        ds_name = f"sregym-img-loader-{uuid.uuid4().hex[:6]}"
        manifest = f"""\
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: {ds_name}
  namespace: {ns}
spec:
  selector:
    matchLabels:
      app: {ds_name}
  template:
    metadata:
      labels:
        app: {ds_name}
    spec:
      tolerations:
      - operator: Exists
      containers:
      - name: loader
        image: docker:cli
        command: ["sleep", "infinity"]
        volumeMounts:
        - name: docker-sock
          mountPath: /var/run/docker.sock
      volumes:
      - name: docker-sock
        hostPath:
          path: /var/run/docker.sock
"""
        try:
            subprocess.run(
                "kubectl apply -f -", shell=True, input=manifest,
                capture_output=True, text=True, check=True,
            )
            subprocess.run(
                f"kubectl rollout status daemonset/{ds_name} -n {ns} --timeout=120s",
                shell=True, capture_output=True, text=True,
            )
            pods_result = subprocess.run(
                f"kubectl get pods -n {ns} -l app={ds_name} "
                f"-o jsonpath='{{.items[?(@.status.phase==\"Running\")].metadata.name}}'",
                shell=True, capture_output=True, text=True,
            )
            pods = [p for p in pods_result.stdout.strip().strip("'").split() if p]
            for pod in pods:
                r = subprocess.run(
                    f"docker save {image_tag} | kubectl exec -n {ns} -i {pod} -- docker load",
                    shell=True, capture_output=True, text=True,
                )
                if r.returncode == 0:
                    logger.info(f"[GenericBuildMgr] Image loaded via DaemonSet pod {pod}")
                else:
                    logger.warning(f"[GenericBuildMgr] DaemonSet load failed on {pod}: {r.stderr.strip()}")
        finally:
            subprocess.run(
                f"kubectl delete daemonset {ds_name} -n {ns} --ignore-not-found",
                shell=True, capture_output=True,
            )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _make_tag(self, content_hash: str) -> str:
        versioned = hashlib.sha256(
            f"{_DOCKERFILE_VERSION}:{content_hash}".encode()
        ).hexdigest()
        return f"sregym/{self.spec.name}-patched:{self.version}-{versioned[:8]}"

    def _image_exists_locally(self, image_tag: str) -> bool:
        return subprocess.run(
            f"docker image inspect {image_tag}",
            shell=True, capture_output=True,
        ).returncode == 0

    @staticmethod
    def _cluster_platform() -> str:
        import platform as _platform
        try:
            result = subprocess.run(
                "kubectl get nodes -o jsonpath='{.items[0].status.nodeInfo.architecture}'",
                shell=True, capture_output=True, text=True, timeout=10,
            )
            arch = result.stdout.strip().strip("'")
            if arch:
                return f"linux/{arch}"
        except Exception:
            pass
        machine = _platform.machine().lower()
        return "linux/arm64" if machine in ("arm64", "aarch64") else "linux/amd64"

    @staticmethod
    def _hash_dir(directory: Path) -> str:
        h = hashlib.sha256()
        for f in sorted(directory.rglob("*")):
            if f.is_file():
                h.update(str(f.relative_to(directory)).encode())
                h.update(f.read_bytes())
        return h.hexdigest()
