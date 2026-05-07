"""Docker-based sandbox backend for running agent operations in containers."""

from __future__ import annotations

import atexit
import datetime
import json
import logging
import os
import signal
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Callable

from deepagents.backends.protocol import (
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    WriteResult,
)
from deepagents.backends.sandbox import BaseSandbox

# Global registry of live containers for cleanup on exit / SIGINT.
_live_containers: dict[str, str] = {}  # container_id -> container_name


def _cleanup_containers() -> None:
    """Stop and remove all tracked containers."""
    for container_id, name in list(_live_containers.items()):
        try:
            subprocess.run(
                ["docker", "stop", container_id],
                capture_output=True, text=True, check=False, timeout=30,
            )
            subprocess.run(
                ["docker", "rm", container_id],
                capture_output=True, text=True, check=False, timeout=10,
            )
        except Exception:
            pass
        _live_containers.pop(container_id, None)


atexit.register(_cleanup_containers)

# Re-raise SIGINT as KeyboardInterrupt so finally/atexit handlers run
# even if a C extension swallows the default disposition.
_original_sigint = signal.getsignal(signal.SIGINT)


def _sigint_handler(signum, frame):
    """Ensure cleanup runs then re-raise the interrupt."""
    # Restore original handler FIRST to prevent recursive re-entry
    # if another SIGINT arrives during cleanup.
    signal.signal(signal.SIGINT, _original_sigint)
    _cleanup_containers()
    if callable(_original_sigint):
        _original_sigint(signum, frame)
    else:
        raise KeyboardInterrupt


signal.signal(signal.SIGINT, _sigint_handler)


class DockerSandbox(BaseSandbox):
    """Sandbox that runs all agent operations inside a Docker container.

    Model weights and other host directories are bind-mounted, eliminating
    symlink issues and path confusion.

    The agent uses virtual absolute paths (``/foo``) expecting ``/`` to be
    the workspace root — matching ``LocalShellBackend(virtual_mode=True)``
    behaviour.  All filesystem methods translate these to container paths
    (``/workspace/foo``) before delegating to ``BaseSandbox``.
    """

    _CONTAINER_ROOT = "/workspace"

    def __init__(
        self,
        host_workspace: str,
        image: str,
        gpus: str | None = None,
        default_timeout: int = 300,
        start_timeout: int = 120,
        max_output_bytes: int = 100_000,
        env: dict[str, str] | None = None,
        bind_mounts: list[tuple[str, str, bool]] | None = None,
        passthrough_paths: list[str] | None = None,
        log_path: str | Path | None = None,
        extra_init_commands: list[str] | None = None,
        setup_fns: list[Callable[["DockerSandbox"], None]] | None = None,
    ) -> None:
        """Initialize Docker sandbox configuration.

        Args:
            host_workspace: Host path to mount as /workspace in the container.
            image: Docker image to use (caller must supply; backends provide
                their own default).
            gpus: GPU device spec for --gpus flag, or None to skip --gpus
                entirely (e.g. for non-CUDA backends).
            default_timeout: Default command timeout in seconds.
            start_timeout: Timeout in seconds for the initial ``docker run``.
                This bounds hidden image pulls or Docker daemon stalls before
                the first agent has a chance to start.
            max_output_bytes: Maximum output bytes before truncation.
            env: Environment variables to set in the container.
            bind_mounts: List of (host_path, container_path, readonly) tuples.
            passthrough_paths: Container paths outside /workspace that should
                not be rewritten by virtual-path translation (e.g. ``["/model"]``).
            log_path: File path to log docker commands to. If None, no logging.
            extra_init_commands: Additional bash commands to run inside the
                container after the default ``pip install uv`` step.  Unlike
                ``uv``, failures here **raise RuntimeError** — use this for
                commands that must succeed (e.g. installing a CLI binary the
                loop depends on).
        """
        self._host_workspace = host_workspace
        self._image = image
        self._gpus = gpus
        self._default_timeout = default_timeout
        self._start_timeout = start_timeout
        self._max_output_bytes = max_output_bytes
        self._env = env or {}
        self._bind_mounts = bind_mounts or []
        self._container_id: str | None = None
        self._logger = self._setup_logger(log_path)
        self._extra_init_commands: list[str] = list(extra_init_commands or [])
        self._setup_fns: list[Callable[["DockerSandbox"], None]] = list(setup_fns or [])

        # Container paths outside /workspace that _vpath must not rewrite.
        self._passthrough_prefixes: list[str] = list(passthrough_paths or [])

    @staticmethod
    def _setup_logger(log_path: str | Path | None) -> logging.Logger | None:
        if log_path is None:
            return None
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger(f"docker_sandbox.{uuid.uuid4().hex[:8]}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        handler = logging.FileHandler(str(log_path))
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        return logger

    def _log_cmd(self, cmd: list[str], result: subprocess.CompletedProcess | None = None, error: str | None = None) -> None:
        if self._logger is None:
            return
        self._logger.info("CMD: %s", " ".join(cmd))
        if result is not None:
            self._logger.info("  exit_code=%d", result.returncode)
            if result.stdout and result.stdout.strip():
                self._logger.info("  stdout: %s", result.stdout.strip()[:1000])
            if result.stderr and result.stderr.strip():
                self._logger.info("  stderr: %s", result.stderr.strip()[:1000])
        if error:
            self._logger.info("  error: %s", error)

    # -- virtual-path translation ------------------------------------------
    #
    # The agent emits paths rooted at "/" (virtual workspace root).
    # BaseSandbox's filesystem helpers pass those literally into shell
    # commands that run inside the container, where the workspace lives at
    # /workspace.  We intercept every path-taking method to prepend the
    # container root.

    def _vpath(self, path: str) -> str:
        """Translate a virtual absolute path to a container path."""
        if path.startswith(self._CONTAINER_ROOT + "/") or path == self._CONTAINER_ROOT:
            return path  # already absolute inside the container
        # Preserve paths that match non-workspace mounts (e.g. /model)
        for prefix in self._passthrough_prefixes:
            if path == prefix or path.startswith(prefix + "/"):
                return path
        if path.startswith("/"):
            return self._CONTAINER_ROOT + path
        return path  # relative — resolved against workdir by the shell

    def ls_info(self, path: str):
        return super().ls_info(self._vpath(path))

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> str:
        return super().read(self._vpath(file_path), offset, limit)

    def write(self, file_path: str, content: str) -> WriteResult:
        """Write a file into the container using docker cp.

        Overrides ``BaseSandbox.write`` which inlines the content into a shell
        command.  For large files this exceeds the OS argument-size limit
        (``E2BIG``).  Using ``docker cp`` via a temp file avoids the limit.
        """
        if self._container_id is None:
            raise RuntimeError("Container not started — call start() first")

        container_path = self._vpath(file_path)

        # Ensure parent directory exists inside the container
        parent = str(Path(container_path).parent)
        mkdir_cmd = ["docker", "exec", self._container_id, "mkdir", "-p", parent]
        subprocess.run(mkdir_cmd, capture_output=True, check=False)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".tmp", delete=True) as tmp:
            tmp.write(content)
            tmp.flush()
            cp_cmd = ["docker", "cp", tmp.name, f"{self._container_id}:{container_path}"]
            self._log_cmd(cp_cmd)
            result = subprocess.run(cp_cmd, capture_output=True, text=True, check=False)
            self._log_cmd(cp_cmd, result)

        if result.returncode != 0:
            return WriteResult(path=file_path, error=result.stderr.strip())
        return WriteResult(path=file_path)

    def edit(self, file_path: str, old_string: str, new_string: str,
             replace_all: bool = False) -> EditResult:
        return super().edit(self._vpath(file_path), old_string, new_string, replace_all)

    def glob_info(self, pattern: str, path: str = "/"):
        return super().glob_info(pattern, self._vpath(path))

    def grep_raw(self, pattern: str, path: str | None = None,
                 glob: str | None = None):
        # Check container is still running before issuing grep; a dead
        # container causes docker-exec to emit an error on stderr that the
        # parent parser cannot parse (e.g. "No such container").
        if self._container_id is not None:
            check = subprocess.run(
                ["docker", "inspect", "--format={{.State.Running}}", self._container_id],
                capture_output=True, text=True, check=False,
            )
            if check.returncode != 0 or "true" not in check.stdout.lower():
                raise RuntimeError(
                    f"Docker container {self._container_id} is no longer running"
                )
        return super().grep_raw(
            pattern,
            self._vpath(path) if path is not None else self._CONTAINER_ROOT,
            glob,
        )

    @staticmethod
    def _resolve_gpu_device(gpus: str) -> str:
        """Resolve the ``--gpus`` device spec using ``CUDA_VISIBLE_DEVICES``.

        When *gpus* is ``"all"`` **and** the ``CUDA_VISIBLE_DEVICES``
        environment variable is set, we pick the first visible device and
        return a ``device=<physical_id>`` string so that exactly one GPU is
        forwarded into the container.  Otherwise the original *gpus* value is
        returned unchanged.
        """
        cuda_visible = os.environ.get("CUDA_VISIBLE_DEVICES")
        if cuda_visible:
            devices = [d.strip() for d in cuda_visible.split(",") if d.strip()]
            if devices:
                # Use the first device listed in CUDA_VISIBLE_DEVICES
                return f"device={devices[0]}"
        # Fallback: pass through as-is (e.g. user explicitly set gpus="device=0")
        return gpus

    def start(self) -> None:
        """Start the Docker container."""
        self._container_name = f"vibeserve-{uuid.uuid4().hex[:12]}"
        cmd = [
            "docker", "run", "-d",
            "--name", self._container_name,
            "-v", f"{self._host_workspace}:/workspace",
        ]
        if self._gpus is not None:
            gpu_spec = self._resolve_gpu_device(self._gpus)
            cmd.extend(["--gpus", gpu_spec])

        for host_path, container_path, readonly in self._bind_mounts:
            mount = f"{host_path}:{container_path}"
            if readonly:
                mount += ":ro"
            cmd.extend(["-v", mount])

        for key, value in self._env.items():
            cmd.extend(["-e", f"{key}={value}"])

        cmd.extend([
            "--workdir", "/workspace",
            self._image,
            "sleep", "infinity",
        ])

        self._log_cmd(cmd)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=self._start_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            self._log_cmd(
                cmd,
                error=f"docker run timed out after {self._start_timeout}s",
            )
            raise RuntimeError(
                "Timed out starting Docker container after "
                f"{self._start_timeout}s. If this is the first run, pre-pull "
                f"the image with `docker pull {self._image}`; otherwise check "
                "Docker daemon health and GPU runtime configuration."
            ) from exc
        self._log_cmd(cmd, result)
        if result.returncode != 0:
            container_id = result.stdout.strip()
            if container_id:
                rm_cmd = ["docker", "rm", container_id]
                rm_result = subprocess.run(
                    rm_cmd,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self._log_cmd(rm_cmd, rm_result)
            raise RuntimeError(
                f"Failed to start Docker container (exit {result.returncode}):\n"
                f"  stdout: {result.stdout.strip()}\n"
                f"  stderr: {result.stderr.strip()}\n"
                f"  cmd: {' '.join(cmd)}"
            )
        self._container_id = result.stdout.strip()
        _live_containers[self._container_id] = self._container_name

        # Save metadata for vibeserve-shell to reconstruct the environment
        self._metadata = {
            "image": self._image,
            "gpus": self._gpus,
            "bind_mounts": [
                [host, container, ro]
                for host, container, ro in self._bind_mounts
            ],
            "env": dict(self._env),
            "symlink_commands": [],
        }
        self._save_metadata()

        # Install uv (with timeout to avoid hanging on network issues)
        uv_cmd = ["docker", "exec", self._container_id, "bash", "-c", "pip install uv"]
        try:
            uv_result = subprocess.run(
                uv_cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            self._log_cmd(uv_cmd, uv_result)
        except subprocess.TimeoutExpired:
            self._log_cmd(uv_cmd, error="pip install uv timed out after 120s")
            # Non-fatal: uv is optional, the agent can still use pip directly

        # Run any additional init commands (e.g. installing a CLI binary).
        # Unlike uv, these are required — a failure raises RuntimeError.
        for init_cmd_str in self._extra_init_commands:
            init_cmd = [
                "docker", "exec", self._container_id,
                "bash", "-c", init_cmd_str,
            ]
            self._log_cmd(init_cmd)
            try:
                init_result = subprocess.run(
                    init_cmd,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=600,
                )
                self._log_cmd(init_cmd, init_result)
                if init_result.returncode != 0:
                    raise RuntimeError(
                        f"Container init command failed (exit {init_result.returncode}):\n"
                        f"  command: {init_cmd_str}\n"
                        f"  stdout: {init_result.stdout.strip()[:500]}\n"
                        f"  stderr: {init_result.stderr.strip()[:500]}"
                    )
            except subprocess.TimeoutExpired:
                self._log_cmd(init_cmd, error=f"init command timed out after 600s: {init_cmd_str}")
                raise RuntimeError(
                    f"Container init command timed out after 600s: {init_cmd_str}"
                )

        # Run caller-supplied setup functions.  These re-execute on every
        # restart (so e.g. docker symlinks survive ``reselect_device``).
        for fn in self._setup_fns:
            fn(self)

    def _save_metadata(self) -> None:
        """Write metadata to the host workspace (best-effort)."""
        try:
            metadata_path = Path(self._host_workspace) / ".docker_metadata.json"
            metadata_path.write_text(json.dumps(self._metadata, indent=2))
        except OSError:
            pass  # Non-fatal: workspace dir may not exist in tests

    def save_symlink_commands(self, symlink_commands: list[str]) -> None:
        """Update the metadata file with symlink commands for vibeserve-shell."""
        self._metadata["symlink_commands"] = symlink_commands
        self._save_metadata()

    def stop(self) -> None:
        """Stop and remove the Docker container. Idempotent."""
        if self._container_id is None:
            return

        container_id = self._container_id
        self._container_id = None
        _live_containers.pop(container_id, None)

        stop_cmd = ["docker", "stop", container_id]
        stop_result = subprocess.run(
            stop_cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        self._log_cmd(stop_cmd, stop_result)
        rm_cmd = ["docker", "rm", container_id]
        rm_result = subprocess.run(
            rm_cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        self._log_cmd(rm_cmd, rm_result)

    @property
    def id(self) -> str:
        """Return sandbox identifier."""
        if self._container_id:
            return self._container_name
        return "vibeserve-not-started"

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Execute a command inside the Docker container."""
        if self._container_id is None:
            raise RuntimeError("Container not started — call start() first")

        effective_timeout = timeout if timeout is not None else self._default_timeout

        exec_cmd = [
            "docker", "exec",
            "-w", "/workspace",
            self._container_id,
            "bash", "-c", command,
        ]
        self._log_cmd(exec_cmd)
        try:
            result = subprocess.run(
                exec_cmd,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired:
            self._log_cmd(exec_cmd, error=f"timeout after {effective_timeout}s")
            return ExecuteResponse(
                output=f"Command timed out after {effective_timeout}s",
                exit_code=-1,
                truncated=False,
            )
        self._log_cmd(exec_cmd, result)

        # When docker-exec itself fails (e.g. container removed), the error
        # lands in stderr with nothing in stdout.  Treat this as a container-
        # level error so callers that parse stdout don't choke on it.
        if result.returncode != 0 and not result.stdout and result.stderr:
            return ExecuteResponse(
                output=result.stderr.strip(),
                exit_code=result.returncode,
                truncated=False,
            )

        output = result.stdout + result.stderr
        truncated = False

        if len(output) > self._max_output_bytes:
            output = output[: self._max_output_bytes] + f"\n... [truncated, {len(result.stdout + result.stderr) - self._max_output_bytes} bytes omitted]"
            truncated = True

        return ExecuteResponse(
            output=output,
            exit_code=result.returncode,
            truncated=truncated,
        )

    def upload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        """Upload files into the container using docker cp."""
        if self._container_id is None:
            raise RuntimeError("Container not started — call start() first")

        results: list[FileUploadResponse] = []

        for path, content in files:
            with tempfile.NamedTemporaryFile(delete=True) as tmp:
                tmp.write(content)
                tmp.flush()

                container_path = self._vpath(path)
                # Ensure parent dir exists
                parent = str(Path(container_path).parent)
                mkdir_cmd = ["docker", "exec", self._container_id, "mkdir", "-p", parent]
                mkdir_result = subprocess.run(
                    mkdir_cmd,
                    capture_output=True,
                    check=False,
                )
                self._log_cmd(mkdir_cmd)

                cp_cmd = ["docker", "cp", tmp.name, f"{self._container_id}:{container_path}"]
                result = subprocess.run(
                    cp_cmd,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self._log_cmd(cp_cmd, result)

                if result.returncode != 0:
                    results.append(FileUploadResponse(path=path, error="permission_denied"))
                else:
                    results.append(FileUploadResponse(path=path))

        return results

    def download_files(
        self,
        paths: list[str],
    ) -> list[FileDownloadResponse]:
        """Download files from the container using docker cp."""
        if self._container_id is None:
            raise RuntimeError("Container not started — call start() first")

        results: list[FileDownloadResponse] = []

        for path in paths:
            container_path = self._vpath(path)

            with tempfile.NamedTemporaryFile(delete=True, suffix=Path(path).suffix) as tmp:
                tmp_path = tmp.name

            cp_cmd = ["docker", "cp", f"{self._container_id}:{container_path}", tmp_path]
            result = subprocess.run(
                cp_cmd,
                capture_output=True,
                text=True,
                check=False,
            )
            self._log_cmd(cp_cmd, result)

            if result.returncode != 0:
                results.append(FileDownloadResponse(path=path, error="file_not_found"))
            else:
                try:
                    content = Path(tmp_path).read_bytes()
                    results.append(FileDownloadResponse(path=path, content=content))
                finally:
                    Path(tmp_path).unlink(missing_ok=True)

        return results

    def __enter__(self) -> DockerSandbox:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
