"""Docker-based sandbox backend for running agent operations in containers."""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from deepagents.backends.protocol import (
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    WriteResult,
)
from deepagents.backends.sandbox import BaseSandbox

from vs_sandbox.lifecycle import SandboxLifecycle, SandboxLifecycleHooks

if TYPE_CHECKING:
    from types import FrameType

# Global registry of live containers for cleanup on exit / SIGINT.
_live_containers: dict[str, str] = {}  # container_id -> container_name

# Environment variables whose *values* must never leave this process.
# Credentials reach the container as ``-e NAME=VALUE`` arguments on
# ``docker run``, and every sink that records a command or the env dict is
# durable and readable: the command log lives beside the run's other logs, the
# metadata file is written into the bind-mounted workspace the agent edits, and
# startup errors are surfaced to the caller.  Endpoint variables such as
# ``*_BASE_URL`` stay visible because they are diagnostics, not secrets.
_SECRET_ENV_NAME_PATTERN = re.compile(
    r"AUTH|TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL|HEADERS",
    re.IGNORECASE,
)
_REDACTED_VALUE = "<redacted>"

#: HOME of the non-root ``agent`` user baked into the agent image. The image
#: creates this user with a real HOME; nothing here creates it.
AGENT_HOME = "/home/agent"

_AGENT_USER = "agent"
_ROOT_SETUP_TIMEOUT_S = 60


def _is_secret_env_name(name: str) -> bool:
    """Report whether *name* identifies a credential-bearing variable."""
    return _SECRET_ENV_NAME_PATTERN.search(name) is not None


def _redacted_command(cmd: list[str]) -> list[str]:
    """Return *cmd* with the values of credential ``-e NAME=VALUE`` flags masked."""
    redacted: list[str] = []
    for index, argument in enumerate(cmd):
        name, separator, _ = argument.partition("=")
        if separator and index > 0 and cmd[index - 1] == "-e" and _is_secret_env_name(name):
            redacted.append(f"{name}={_REDACTED_VALUE}")
        else:
            redacted.append(argument)
    return redacted


def _non_secret_env(env: dict[str, str]) -> dict[str, str]:
    """Return *env* without credential entries, for the on-disk metadata file."""
    return {name: value for name, value in env.items() if not _is_secret_env_name(name)}


def _cleanup_containers() -> None:
    """Stop and remove all tracked containers."""
    for container_id, _name in list(_live_containers.items()):
        try:  # noqa: SIM105  # tracked: #288
            subprocess.run(  # noqa: S603  # tracked: #288
                ["docker", "stop", container_id],  # noqa: S607  # tracked: #288
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except Exception:  # noqa: BLE001, S110  # tracked: #288
            pass
        try:
            removed = subprocess.run(  # noqa: S603  # tracked: #288
                ["docker", "rm", "-f", container_id],  # noqa: S607  # tracked: #288
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except Exception:  # noqa: BLE001, S112  # tracked: #288
            continue
        if removed.returncode == 0 or "No such container" in (removed.stderr or ""):
            _live_containers.pop(container_id, None)


atexit.register(_cleanup_containers)

# Re-raise SIGINT as KeyboardInterrupt so finally/atexit handlers run
# even if a C extension swallows the default disposition.
_original_sigint = signal.getsignal(signal.SIGINT)


def _sigint_handler(signum: int, frame: FrameType | None) -> None:
    """Re-raise the interrupt and let normal unwinding precede cleanup.

    Container cleanup is registered with ``atexit``. Removing the editor
    container here races with caller ``finally`` blocks that still need to run
    inside it, notably VibeSys' bind-mount ownership repair. Restoring the
    prior handler and re-raising first lets those blocks finish; process exit
    then invokes ``_cleanup_containers`` without leaking the container.
    """
    # Restore original handler FIRST to prevent recursive re-entry
    # while the interrupt unwinds through caller cleanup.
    signal.signal(signal.SIGINT, _original_sigint)
    # signal.Handlers and signal.Signals are IntEnum, so the int/None test is
    # the exact complement of callable() for anything getsignal() can return.
    if _original_sigint is None or isinstance(_original_sigint, int):
        raise KeyboardInterrupt
    _original_sigint(signum, frame)


signal.signal(signal.SIGINT, _sigint_handler)


class DockerSandbox(BaseSandbox):
    """Sandbox that runs all agent operations inside a Docker container.

    Model weights and other host directories are bind-mounted, eliminating
    symlink issues and path confusion.

    The agent uses virtual absolute paths (``/foo``) expecting ``/`` to be
    the workspace root — matching ``LocalShellBackend(virtual_mode=True)``
    behaviour.  All filesystem methods translate these to container paths
    (``/workspace/foo``) before delegating to ``BaseSandbox``.

    The container starts from a prebuilt agent image (shipped CLIs, toolchains,
    and a non-root ``agent`` user with a real HOME already baked in), so no
    install runs at start. Only two things still happen at start, both as
    root: the image's ``agent`` user is remapped to the host uid/gid so
    bind-mounted workspace writes come back owned by the host user, and any
    staged provider credentials are copied into the agent's HOME. Every other
    command, including the agent's own, runs as the image's default user.
    """

    _CONTAINER_ROOT = "/workspace"

    def __init__(  # noqa: D417, PLR0913  # tracked: #288
        self,
        host_workspace: str,
        image: str,
        gpus: str | None = None,
        devices: list[str] | None = None,
        group_add: list[str] | None = None,
        entrypoint: str | None = None,
        shm_size: str | None = None,
        auto_remove: bool = False,  # noqa: FBT001, FBT002  # tracked: #288
        default_timeout: int = 300,
        start_timeout: int = 120,
        max_output_bytes: int = 100_000,
        env: dict[str, str] | None = None,
        bind_mounts: list[tuple[str, str, bool]] | None = None,
        passthrough_paths: list[str] | None = None,
        log_path: str | Path | None = None,
        agent_uid: int | None = None,
        agent_gid: int | None = None,
        auth_files: list[tuple[str, str]] | None = None,
        lifecycle_hooks: list[SandboxLifecycleHooks] | None = None,
    ) -> None:
        """Initialize Docker sandbox configuration.

        Args:
            host_workspace: Host path to mount as /workspace in the container.
            image: Docker image to use (caller must supply; backends provide
                their own default).
            gpus: GPU device spec for --gpus flag, or None to skip --gpus
                entirely (e.g. for non-CUDA backends).
            devices: Host device paths (e.g. ``["/dev/neuron0"]``) to forward
                with ``--device``.  Used by non-CUDA accelerators (AWS Neuron)
                that the NVIDIA container runtime's ``--gpus`` cannot expose.
            group_add: Supplementary groups to add the container user to
                (emits ``--group-add``).  Required by accelerators whose
                device nodes are group-owned rather than world-accessible —
                AMD ROCm needs ``video`` and ``render`` to open ``/dev/kfd``
                and ``/dev/dri/*``.
            entrypoint: Override the image ``ENTRYPOINT`` (emits
                ``--entrypoint``).  Pass ``""`` to *clear* a baked-in
                entrypoint so the container runs ``sleep infinity`` directly
                — required for images like the AWS Neuron DLC whose entrypoint
                would otherwise launch a model server and ignore our command.
                ``None`` (default) leaves the image entrypoint untouched.
            shm_size: Value for ``--shm-size`` (e.g. ``"16g"``).  Docker's
                default ``/dev/shm`` is 64 MB, which ML compilers/runtimes
                (e.g. ``neuronx-cc``, PyTorch dataloaders) exhaust with
                "No space left on device".  ``None`` (default) uses Docker's
                default.
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
            agent_uid: Host uid the image's ``agent`` user is remapped to at
                start, so files the agent writes to the bind-mounted
                workspace are owned by the host user. Defaults to this
                process's uid. The remap is skipped when the image's
                ``agent`` user already has this uid.
            agent_gid: Host gid the image's ``agent`` group is remapped to,
                analogous to *agent_uid*. Defaults to this process's gid.
            auth_files: ``(staged_path, destination_path)`` pairs copied into
                the container as root at start, then chowned to ``agent``.
                *staged_path* is a read-only staging location already reached
                through *bind_mounts* (conventionally under
                ``/opt/vibesys-auth``); *destination_path* is where the CLI
                expects to find it, conventionally under :data:`AGENT_HOME`.
            lifecycle_hooks: Trusted extensions invoked in order after
                built-in initialization and before the sandbox becomes ready.
                They run again after every container recreation.
        """
        self._host_workspace = host_workspace
        self._image = image
        self._gpus = gpus
        self._devices: list[str] = list(devices or [])
        self._group_add: list[str] = list(group_add or [])
        self._entrypoint = entrypoint
        self._shm_size = shm_size
        self._auto_remove = auto_remove
        self._default_timeout = default_timeout
        self._start_timeout = start_timeout
        self._max_output_bytes = max_output_bytes
        self._env = env or {}
        self._bind_mounts = bind_mounts or []
        self._container_id: str | None = None
        self._logger = self._setup_logger(log_path)
        self._agent_uid = agent_uid if agent_uid is not None else os.getuid()
        self._agent_gid = agent_gid if agent_gid is not None else os.getgid()
        self._auth_files: list[tuple[str, str]] = list(auth_files or [])
        self._lifecycle = SandboxLifecycle(lifecycle_hooks)

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

    def _log_cmd(
        self,
        cmd: list[str],
        result: subprocess.CompletedProcess[str] | None = None,
        error: str | None = None,
    ) -> None:
        if self._logger is None:
            return
        self._logger.info("CMD: %s", " ".join(_redacted_command(cmd)))
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

    def ls_info(self, path: str):  # noqa: ANN201, D102  # tracked: #288
        return super().ls_info(self._vpath(path))

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> str:  # noqa: D102  # tracked: #288
        return super().read(self._vpath(file_path), offset, limit)

    def write(self, file_path: str, content: str) -> WriteResult:
        """Write a file into the container using docker cp.

        Overrides ``BaseSandbox.write`` which inlines the content into a shell
        command.  For large files this exceeds the OS argument-size limit
        (``E2BIG``).  Using ``docker cp`` via a temp file avoids the limit.
        """
        if self._container_id is None:
            raise RuntimeError("Container not started — call start() first")  # noqa: TRY003  # tracked: #288

        container_path = self._vpath(file_path)

        # Ensure parent directory exists inside the container
        parent = str(Path(container_path).parent)
        mkdir_cmd = ["docker", "exec", self._container_id, "mkdir", "-p", parent]
        subprocess.run(mkdir_cmd, capture_output=True, check=False)  # noqa: S603  # tracked: #288

        with tempfile.NamedTemporaryFile(mode="w", suffix=".tmp", delete=True) as tmp:
            tmp.write(content)
            tmp.flush()
            cp_cmd = ["docker", "cp", tmp.name, f"{self._container_id}:{container_path}"]
            self._log_cmd(cp_cmd)
            result = subprocess.run(cp_cmd, capture_output=True, text=True, check=False)  # noqa: S603  # tracked: #288
            self._log_cmd(cp_cmd, result)

        if result.returncode != 0:
            return WriteResult(path=file_path, error=result.stderr.strip())
        return WriteResult(path=file_path)

    def edit(  # noqa: D102  # tracked: #288
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,  # noqa: FBT001, FBT002  # tracked: #288
    ) -> EditResult:
        return super().edit(self._vpath(file_path), old_string, new_string, replace_all)

    def glob_info(self, pattern: str, path: str = "/"):  # noqa: ANN201, D102  # tracked: #288
        return super().glob_info(pattern, self._vpath(path))

    def grep_raw(self, pattern: str, path: str | None = None, glob: str | None = None):  # noqa: ANN201, D102  # tracked: #288
        # Check container is still running before issuing grep; a dead
        # container causes docker-exec to emit an error on stderr that the
        # parent parser cannot parse (e.g. "No such container").
        if self._container_id is not None:
            check = subprocess.run(  # noqa: S603  # tracked: #288
                ["docker", "inspect", "--format={{.State.Running}}", self._container_id],  # noqa: S607  # tracked: #288
                capture_output=True,
                text=True,
                check=False,
            )
            if check.returncode != 0 or "true" not in check.stdout.lower():
                raise RuntimeError(f"Docker container {self._container_id} is no longer running")  # noqa: TRY003  # tracked: #288
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

    def start(self) -> None:  # noqa: C901, PLR0912  # tracked: #288
        """Start the Docker container."""
        self._container_name = f"vibesys-{uuid.uuid4().hex[:12]}"
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            self._container_name,
            "-v",
            f"{self._host_workspace}:/workspace",
        ]
        if self._auto_remove:
            # Auto-remove the container (and its overlay, which can hold many GB
            # of compiled artifacts) whenever it goes away — including when the
            # framework process is killed and the container is later stopped,
            # which the graceful stop()/rm path would miss.
            cmd.append("--rm")
        if self._gpus is not None:
            gpu_spec = self._resolve_gpu_device(self._gpus)
            cmd.extend(["--gpus", gpu_spec])

        for device in self._devices:
            cmd.extend(["--device", device])

        for group in self._group_add:
            cmd.extend(["--group-add", group])

        if self._entrypoint is not None:
            cmd.extend(["--entrypoint", self._entrypoint])

        if self._shm_size is not None:
            cmd.extend(["--shm-size", self._shm_size])

        for host_path, container_path, readonly in self._bind_mounts:
            mount = f"{host_path}:{container_path}"
            if readonly:
                mount += ":ro"
            cmd.extend(["-v", mount])

        for key, value in self._env.items():
            cmd.extend(["-e", f"{key}={value}"])

        cmd.extend(
            [
                "--workdir",
                "/workspace",
                self._image,
                "sleep",
                "infinity",
            ]
        )

        self._log_cmd(cmd)
        try:
            result = subprocess.run(  # noqa: S603  # tracked: #288
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
            raise RuntimeError(  # noqa: TRY003  # tracked: #288
                "Timed out starting Docker container after "
                f"{self._start_timeout}s. If this is the first run, pre-pull "
                f"the image with `docker pull {self._image}`; otherwise check "
                "Docker daemon health and GPU runtime configuration."
            ) from exc
        self._log_cmd(cmd, result)
        if result.returncode != 0:
            container_id = result.stdout.strip()
            if container_id:
                self._container_id = container_id
                _live_containers[container_id] = self._container_name
                self._discard_started_container()
            raise RuntimeError(  # noqa: TRY003  # tracked: #288
                f"Failed to start Docker container (exit {result.returncode}):\n"
                f"  stdout: {result.stdout.strip()}\n"
                f"  stderr: {result.stderr.strip()}\n"
                f"  cmd: {' '.join(_redacted_command(cmd))}"
            )
        self._container_id = result.stdout.strip()
        _live_containers[self._container_id] = self._container_name

        try:
            self._initialize_started_container()
        except BaseException:
            self._discard_started_container()
            raise

    def _initialize_started_container(self) -> None:
        """Finish startup after Docker has created and registered the container."""
        container_id = self._container_id
        if container_id is None:
            raise RuntimeError("Container was not created before initialization")  # noqa: TRY003  # tracked: #288

        # Save metadata for vibesys-shell to reconstruct the environment
        self._metadata = {
            "image": self._image,
            "gpus": self._gpus,
            "devices": list(self._devices),
            "group_add": list(self._group_add),
            "entrypoint": self._entrypoint,
            "shm_size": self._shm_size,
            "bind_mounts": [[host, container, ro] for host, container, ro in self._bind_mounts],
            # Credentials are omitted rather than masked: the metadata file is
            # written into the agent-visible workspace, and a masked value
            # would rebuild a broken container that looks authenticated.
            "env": _non_secret_env(self._env),
            "symlink_commands": [],
        }
        self._save_metadata()

        self._remap_agent_user(container_id)
        self._copy_auth_files(container_id)

        self._lifecycle.before_ready(self)

    def _run_as_root(self, container_id: str, script: str, *, what: str) -> None:
        """Run *script* as root inside the container; raise on failure or timeout.

        These steps (the uid/gid remap, the auth-file copy) are required: the
        agent image ships no root fallback, so a failure here would otherwise
        surface much later as a confusing permission error deep in a turn.
        """
        cmd = ["docker", "exec", "-u", "root", container_id, "bash", "-c", script]
        self._log_cmd(cmd)
        try:
            result = subprocess.run(  # noqa: S603  # tracked: #288
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=_ROOT_SETUP_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            self._log_cmd(cmd, error=f"{what} timed out after {_ROOT_SETUP_TIMEOUT_S}s")
            raise RuntimeError(  # noqa: TRY003  # tracked: #288
                f"{what} timed out after {_ROOT_SETUP_TIMEOUT_S}s"
            ) from exc
        self._log_cmd(cmd, result)
        if result.returncode != 0:
            raise RuntimeError(  # noqa: TRY003  # tracked: #288
                f"{what} failed (exit {result.returncode}):\n"
                f"  stdout: {result.stdout.strip()[:500]}\n"
                f"  stderr: {result.stderr.strip()[:500]}"
            )

    def _current_agent_ids(self, container_id: str) -> tuple[int, int] | None:
        """Return the image's ``agent`` user's current (uid, gid), if resolvable."""
        result = subprocess.run(  # noqa: S603  # tracked: #288
            [  # noqa: S607  # tracked: #288
                "docker",
                "exec",
                container_id,
                "sh",
                "-c",
                f"id -u {_AGENT_USER} && id -g {_AGENT_USER}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_ROOT_SETUP_TIMEOUT_S,
        )
        if result.returncode != 0:
            return None
        lines = result.stdout.split()
        if len(lines) != 2:  # noqa: PLR2004  # tracked: #288
            return None
        try:
            return int(lines[0]), int(lines[1])
        except ValueError:
            return None

    def _remap_agent_user(self, container_id: str) -> None:
        """Remap the image's ``agent`` user to the configured host uid/gid.

        Skipped when the image's ``agent`` user already carries these ids, so
        a host user that happens to already be uid/gid 1000 (the image's
        baked-in default) pays no extra ``docker exec``.
        """
        current = self._current_agent_ids(container_id)
        if current == (self._agent_uid, self._agent_gid):
            return
        script = (
            f"usermod -o -u {self._agent_uid} {_AGENT_USER} && "
            f"groupmod -o -g {self._agent_gid} {_AGENT_USER} && "
            f"chown -R {_AGENT_USER}:{_AGENT_USER} {AGENT_HOME}"
        )
        self._run_as_root(container_id, script, what="agent user id remap")

    def _copy_auth_files(self, container_id: str) -> None:
        """Copy staged provider credentials into the agent's writable HOME.

        Copying from the read-only staging mount into the agent's own
        writable layer, rather than mounting the destination itself, keeps
        session/history writes inside the disposable container.
        """
        for source, destination in self._auth_files:
            parent = str(Path(destination).parent)
            script = (
                f"mkdir -p {shlex.quote(parent)} && "
                f"cp -a {shlex.quote(source)} {shlex.quote(destination)} && "
                f"chown -R {_AGENT_USER}:{_AGENT_USER} {shlex.quote(destination)}"
            )
            self._run_as_root(container_id, script, what=f"auth file copy to {destination}")

    def _discard_started_container(self) -> None:
        """Best-effort rollback for a container whose startup did not finish."""
        if self._container_id is None:
            return

        container_id = self._container_id
        if self._stop_and_remove_container(container_id, suppress_errors=True):
            self._container_id = None
            _live_containers.pop(container_id, None)

    def _stop_and_remove_container(self, container_id: str, *, suppress_errors: bool) -> bool:
        """Stop and remove a container, retaining ownership until removal succeeds."""
        cleanup_error: Exception | None = None
        commands = (
            (["docker", "stop", container_id], 30),
            (["docker", "rm", "-f", container_id], 10),
        )
        removed = False
        for cmd, timeout in commands:
            try:
                result = subprocess.run(  # noqa: S603  # tracked: #288
                    cmd,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=timeout,
                )
                self._log_cmd(cmd, result)
                if cmd[1] == "rm":
                    stderr = result.stderr or ""
                    removed = (
                        result.returncode == 0
                        or "No such container" in stderr
                        or ("removal of container" in stderr and "is already in progress" in stderr)
                    )
                    if not removed:
                        cleanup_error = RuntimeError(
                            f"Failed to remove Docker container {container_id} "
                            f"(exit {result.returncode}): {result.stderr.strip()}"
                        )
            except Exception as exc:  # noqa: BLE001  # tracked: #288
                if cmd[1] == "rm":
                    cleanup_error = exc
                try:  # noqa: SIM105  # tracked: #288
                    self._log_cmd(cmd, error=f"container cleanup failed: {exc}")
                except Exception:  # noqa: BLE001, S110  # tracked: #288
                    pass

        if not removed and cleanup_error is not None and not suppress_errors:
            raise cleanup_error
        return removed

    def _save_metadata(self) -> None:
        """Write metadata to the host workspace (best-effort)."""
        try:
            metadata_path = Path(self._host_workspace) / ".docker_metadata.json"
            metadata_path.write_text(json.dumps(self._metadata, indent=2))
        except OSError:
            pass  # Non-fatal: workspace dir may not exist in tests

    def save_symlink_commands(self, symlink_commands: list[str]) -> None:
        """Update the metadata file with symlink commands for vibesys-shell."""
        self._metadata["symlink_commands"] = symlink_commands
        self._save_metadata()

    def stop(self) -> None:
        """Stop and remove the Docker container. Idempotent."""
        if self._container_id is None:
            return

        container_id = self._container_id
        if self._stop_and_remove_container(container_id, suppress_errors=False):
            self._container_id = None
            _live_containers.pop(container_id, None)

    @property
    def container_id(self) -> str:
        """Return the Docker container id backing this sandbox.

        Callers that need to run their own ``docker`` commands against the
        container (a command executor, an exec probe) read the id here rather
        than the private attribute. Raises ``RuntimeError`` when no container
        is running, which is also the state after :meth:`stop`.
        """
        if self._container_id is None:
            raise RuntimeError(  # noqa: TRY003  # tracked: #288
                f"Docker sandbox for {self._host_workspace} has no running "
                "container — call start() first"
            )
        return self._container_id

    @property
    def id(self) -> str:
        """Return sandbox identifier."""
        if self._container_id:
            return self._container_name
        return "vibesys-not-started"

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Execute a command inside the Docker container."""
        if self._container_id is None:
            raise RuntimeError("Container not started — call start() first")  # noqa: TRY003  # tracked: #288

        effective_timeout = timeout if timeout is not None else self._default_timeout

        exec_cmd = [
            "docker",
            "exec",
            "-w",
            "/workspace",
            self._container_id,
            "bash",
            "-c",
            command,
        ]
        self._log_cmd(exec_cmd)
        try:
            result = subprocess.run(  # noqa: PLW1510, S603  # tracked: #288
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
            output = (
                output[: self._max_output_bytes]
                + f"\n... [truncated, {len(result.stdout + result.stderr) - self._max_output_bytes} bytes omitted]"
            )
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
            raise RuntimeError("Container not started — call start() first")  # noqa: TRY003  # tracked: #288

        results: list[FileUploadResponse] = []

        for path, content in files:
            with tempfile.NamedTemporaryFile(delete=True) as tmp:
                tmp.write(content)
                tmp.flush()

                container_path = self._vpath(path)
                # Ensure parent dir exists
                parent = str(Path(container_path).parent)
                mkdir_cmd = ["docker", "exec", self._container_id, "mkdir", "-p", parent]
                subprocess.run(  # noqa: S603  # tracked: #288
                    mkdir_cmd,
                    capture_output=True,
                    check=False,
                )
                self._log_cmd(mkdir_cmd)

                cp_cmd = ["docker", "cp", tmp.name, f"{self._container_id}:{container_path}"]
                result = subprocess.run(  # noqa: S603  # tracked: #288
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
            raise RuntimeError("Container not started — call start() first")  # noqa: TRY003  # tracked: #288

        results: list[FileDownloadResponse] = []

        for path in paths:
            container_path = self._vpath(path)

            with tempfile.NamedTemporaryFile(delete=True, suffix=Path(path).suffix) as tmp:
                tmp_path = tmp.name

            cp_cmd = ["docker", "cp", f"{self._container_id}:{container_path}", tmp_path]
            result = subprocess.run(  # noqa: S603  # tracked: #288
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

    def __enter__(self) -> DockerSandbox:  # noqa: D105  # tracked: #288
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:  # noqa: D105  # tracked: #288
        self.stop()
