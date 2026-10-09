"""Docker-based sandbox backend for running agent operations in containers."""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import shlex
import subprocess
import uuid
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, override

from vs_project.api import atomic_write_bytes
from vs_sandbox.command_execution import execute_command
from vs_sandbox.container_runtime import (
    SYSBOX_DOCKER_RUNTIME,
    require_sysbox_runtime,
    start_nested_daemon,
    workspace_container_root,
)
from vs_sandbox.docker_cli import (
    EXEC_MARKER_ENV,
    SIGNAL_EXEC_SCRIPT,
    DockerCli,
    SubprocessDockerCli,
)
from vs_sandbox.host_resources import HostResourceAccess
from vs_sandbox.host_sandbox import WorkspaceSandbox
from vs_sandbox.lifecycle import SandboxLifecycle, SandboxLifecycleHooks

if TYPE_CHECKING:
    import signal
    import threading
    from collections.abc import Mapping, Sequence

    from vs_sandbox.execution import CommandResult
    from vs_sandbox.host_resources import HostResource

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

#: Label every container carries with the id of the run that started it, so a
#: later reap can list a dead run's leftovers with
#: ``docker ps -a --filter label=vibesys.run-id=<id>``.
RUN_ID_LABEL = "vibesys.run-id"

_AGENT_USER = "agent"
_ROOT_SETUP_TIMEOUT_S = 60
_CONTAINER_IDENTITY_LINE_COUNT = 2
_EXEC_STOP_TIMEOUT_S = 30
_NAMED_REMOVE_TIMEOUT_S = 30


class DockerSandboxNotStartedError(RuntimeError):
    """Raised when a live-container operation is requested before start."""

    @classmethod
    def operation_before_start(cls) -> DockerSandboxNotStartedError:
        """Describe an operation attempted before the sandbox starts."""
        return cls("Container not started — call start() first")

    @classmethod
    def container_id_unavailable(cls, workspace: str) -> DockerSandboxNotStartedError:
        """Describe a container-id read before the sandbox starts."""
        return cls(f"Docker sandbox for {workspace} has no running container — call start() first")


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
        with suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(  # noqa: S603  # lint-waiver: LW-007110 [S603]; fixed docker stop argv is issued without a shell during process cleanup.
                ["docker", "stop", container_id],  # noqa: S607  # lint-waiver: LW-007119 [S607]; Docker is a PATH-resolved runtime dependency.
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        removed = None
        with suppress(OSError, subprocess.TimeoutExpired):
            removed = subprocess.run(  # noqa: S603  # lint-waiver: LW-007111 [S603]; fixed docker rm argv is issued without a shell during process cleanup.
                ["docker", "rm", "-f", container_id],  # noqa: S607  # lint-waiver: LW-007120 [S607]; Docker is a PATH-resolved runtime dependency.
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        if removed is None:
            continue
        if removed.returncode == 0 or "No such container" in (removed.stderr or ""):
            _live_containers.pop(container_id, None)


atexit.register(_cleanup_containers)


def _first_component_below(home: str, destination: str) -> str:
    """Return the first path component of *destination* under *home*.

    ``/home/agent/.codex/auth.json`` gives ``/home/agent/.codex``; a file
    directly under HOME gives itself.
    """
    relative = Path(destination).relative_to(home)
    return str(Path(home) / relative.parts[0])


def _bind_mounts_for_resources(
    resources: Sequence[HostResource],
) -> list[tuple[str, str, bool]]:
    """Lower a host resource list to the ``(host, container, readonly)`` mounts.

    A resource's ``agent_path`` becomes the container-side mount destination;
    an unset ``agent_path`` mounts at the same path the host uses, mirroring
    what the host confinement backends do for a resource they cannot remap.
    An unlisted path is simply never in this list, so it is never mounted.
    """
    return [
        (
            str(resource.path),
            resource.agent_path if resource.agent_path is not None else str(resource.path),
            resource.access is HostResourceAccess.READ_ONLY,
        )
        for resource in resources
    ]


def _agent_path_map(
    host_workspace: str,
    container_root: str,
    resources: Sequence[HostResource],
) -> tuple[tuple[str, str], ...]:
    """Build the ``(host prefix, container prefix)`` table :meth:`DockerSandbox.agent_path` uses.

    Includes the workspace's fixed mapping to *container_root* plus one entry
    per resource (its own host path unless ``agent_path`` overrides it).
    Sorted by host-prefix length, longest first, so a resource nested inside
    another mapped path — or inside the workspace — wins over its ancestor.
    """
    entries = [
        (str(Path(host_workspace)), container_root),
        *(
            (
                str(resource.path),
                resource.agent_path if resource.agent_path is not None else str(resource.path),
            )
            for resource in resources
        ),
    ]
    return tuple(sorted(entries, key=lambda pair: len(pair[0]), reverse=True))


class DockerSandbox(WorkspaceSandbox):
    """Run commands and the agent CLI inside a Docker container.

    Model weights and other host directories are bind-mounted, eliminating
    symlink issues and path confusion.

    The container starts from a prebuilt agent image (shipped CLIs, toolchains,
    and a non-root ``agent`` user with a real HOME already baked in), so no
    install runs at start. Only two things still happen at start, both as
    root: the image's ``agent`` user is remapped to the host uid/gid so
    bind-mounted workspace writes come back owned by the host user, and any
    staged provider credentials are copied into the agent's HOME. Every other
    command, including the agent's own, runs as the image's default user.

    Also a :class:`~vs_sandbox.host_sandbox.WorkspaceSandbox`: the container
    is Docker's own confinement boundary, enforcing the same
    :class:`~vs_sandbox.host_resources.HostResource` list the host backends
    consume, lowered to bind mounts instead of a mount namespace. ``resources``
    is the resource-list construction path; ``bind_mounts`` keeps working
    unchanged for callers that build their own mount tuples directly, and the
    two combine when both are given. :meth:`agent_path` and :attr:`env`
    override the host-only defaults :class:`WorkspaceSandbox` provides, and
    :meth:`wrap` yields a ``docker exec`` prefix rather than a namespace tool.

    ``WorkspaceSandbox`` is a frozen dataclass; this class predates it and
    owns a great deal of mutable state (``self._container_id`` and friends)
    through its own conventional ``__init__``, never calling the generated
    dataclass ``__init__``. A frozen dataclass's generated ``__setattr__``
    only rejects assignment to its own field names (or to any name at all on
    an instance of the dataclass's exact type); an ordinary attribute name
    assigned on a *subclass* instance still goes through unimpeded. None of
    ``WorkspaceSandbox``'s dataclass field names (``workspace``,
    ``read_paths``, ``write_paths``, ``project_path_policy``, ``build_env``)
    are used as attribute names here, so this class's own mutable state never
    collides with that restriction. It does opt out of the generated
    ``__eq__``/``__hash__`` (both would otherwise read those same unset
    fields off ``self`` and raise), reverting to identity comparison.
    """

    __eq__ = object.__eq__
    __hash__ = object.__hash__

    def __repr__(self) -> str:
        """Return a debug repr that never touches the unset dataclass fields."""
        return f"DockerSandbox(image={self._image!r}, container_id={self._container_id!r})"

    @override
    def __init__(
        self,
        host_workspace: str,
        image: str,
        gpus: str | None = None,
        devices: list[str] | None = None,
        group_add: list[str] | None = None,
        entrypoint: str | None = None,
        shm_size: str | None = None,
        auto_remove: bool = False,
        default_timeout: int = 300,
        start_timeout: int = 120,
        max_output_bytes: int = 100_000,
        env: dict[str, str] | None = None,
        bind_mounts: list[tuple[str, str, bool]] | None = None,
        resources: Sequence[HostResource] = (),
        log_path: str | Path | None = None,
        agent_uid: int | None = None,
        agent_gid: int | None = None,
        auth_files: list[tuple[str, str]] | None = None,
        lifecycle_hooks: list[SandboxLifecycleHooks] | None = None,
        docker: DockerCli | None = None,
        docker_in_docker: bool = False,
        same_path_workspace: bool = False,
        run_id: str | None = None,
    ) -> None:
        """Initialize Docker sandbox configuration.

        Args:
            host_workspace: Host path to mount in the container: at
                ``/workspace``, or at its own host path when *docker_in_docker*
                is set.
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
            auto_remove: Ask Docker to remove the container and its writable
                layer whenever it stops.
            default_timeout: Default command timeout in seconds.
            start_timeout: Timeout in seconds for the initial ``docker run``.
                This bounds hidden image pulls or Docker daemon stalls before
                the first agent has a chance to start.
            max_output_bytes: Maximum output characters before truncation.
            env: Environment variables to set in the container.
            bind_mounts: List of (host_path, container_path, readonly) tuples.
            resources: Host resources to enforce, lowered to bind mounts:
                read-only resources mount ``:ro``, read-write ones mount
                writable, and a path with no resource is never mounted. A
                resource's ``agent_path`` becomes the container-side mount
                destination; unset, it mounts at its own host path. Combines
                with *bind_mounts* rather than replacing it, so existing
                callers that build mount tuples directly keep working
                unchanged. Also the source :meth:`agent_path` consults.
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
            docker: The ``docker`` CLI to run commands through; defaults to
                the real binary on ``PATH``. Tests pass a fake daemon.
            docker_in_docker: Run the container under the Sysbox runtime with a
                Docker daemon of its own inside it (see
                :mod:`vs_sandbox.container_runtime`), mounting the workspace at
                its host path. :meth:`start` raises
                ``ContainerRuntimeUnavailableError`` when the host has no
                Sysbox; nothing falls back to the host socket. Sysbox cannot
                forward accelerators, so combining it with *gpus* or
                *devices* is rejected here.
            same_path_workspace: Mount the workspace at its own host path, as
                a docker-in-docker sandbox does, without the Sysbox runtime.
                For an agent that talks to a host-owned broker over a shared
                filesystem: a working directory it sends is then valid on the
                host.
            run_id: Id of the run this container belongs to, recorded as the
                :data:`RUN_ID_LABEL` label. ``None`` leaves the container
                unlabelled.
        """
        if docker_in_docker and (gpus is not None or devices):
            message = (
                "the Sysbox container runtime cannot forward accelerators; "
                "drop gpus/devices for a docker_in_docker task"
            )
            raise ValueError(message)
        self._host_workspace = host_workspace
        self._docker_in_docker = docker_in_docker
        self._run_id = run_id
        #: Where the workspace is mounted in the container.
        self._container_root = workspace_container_root(
            host_workspace, same_path=docker_in_docker or same_path_workspace
        )
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
        self._resources: tuple[HostResource, ...] = tuple(resources)
        self._bind_mounts = list(bind_mounts or []) + _bind_mounts_for_resources(self._resources)
        self._agent_path_map: tuple[tuple[str, str], ...] = _agent_path_map(
            host_workspace, self._container_root, self._resources
        )
        #: The container's own PATH, read once via :attr:`env` and cached for
        #: the sandbox's lifetime; ``None`` until first read.
        self._cached_container_path: str | None = None
        self._container_id: str | None = None
        self._logger = self._setup_logger(log_path)
        self._agent_uid = agent_uid if agent_uid is not None else os.getuid()
        self._agent_gid = agent_gid if agent_gid is not None else os.getgid()
        self._auth_files: list[tuple[str, str]] = list(auth_files or [])
        self._lifecycle = SandboxLifecycle(lifecycle_hooks)
        self._docker: DockerCli = docker if docker is not None else SubprocessDockerCli()

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

    def _start_command(self) -> list[str]:
        """Build the Docker argv for starting this configured sandbox."""
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            self._container_name,
            "-v",
            f"{self._host_workspace}:{self._container_root}",
        ]
        cmd.extend(self._label_arguments())
        cmd.extend(self._container_runtime_arguments())
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
                self._container_root,
                self._image,
                "sleep",
                "infinity",
            ]
        )
        return cmd

    def _label_arguments(self) -> list[str]:
        """Return the ``docker run`` flags that label the container with its run."""
        return [] if self._run_id is None else ["--label", f"{RUN_ID_LABEL}={self._run_id}"]

    def _container_runtime_arguments(self) -> list[str]:
        """Return the ``docker run`` flags a docker-in-docker sandbox adds."""
        return ["--runtime", SYSBOX_DOCKER_RUNTIME] if self._docker_in_docker else []

    def start(self) -> None:
        """Start the Docker container."""
        if self._docker_in_docker:
            require_sysbox_runtime(self._docker)
        self._container_name = f"vibesys-{uuid.uuid4().hex[:12]}"
        cmd = self._start_command()

        self._log_cmd(cmd)
        try:
            result = self._docker.run(cmd, timeout_seconds=self._start_timeout)
        except BaseException as exc:
            # The daemon may have created the container before the client
            # died, timed out, or was interrupted; its name is the only handle.
            self._remove_container_named(self._container_name)
            if not isinstance(exc, subprocess.TimeoutExpired):
                raise
            self._log_cmd(
                cmd,
                error=f"docker run timed out after {self._start_timeout}s",
            )
            message = (
                "Timed out starting Docker container after "
                f"{self._start_timeout}s. If this is the first run, pre-pull "
                f"the image with `docker pull {self._image}`; otherwise check "
                "Docker daemon health and GPU runtime configuration."
            )
            raise RuntimeError(message) from exc
        self._log_cmd(cmd, result)
        if result.returncode != 0:
            container_id = result.stdout.strip()
            if container_id:
                self._container_id = container_id
                _live_containers[container_id] = self._container_name
                self._discard_started_container()
            else:
                # A client that failed after the daemon created the container
                # prints no id; remove it by the name this sandbox chose.
                self._remove_container_named(self._container_name)
            message = (
                f"Failed to start Docker container (exit {result.returncode}):\n"
                f"  stdout: {result.stdout.strip()}\n"
                f"  stderr: {result.stderr.strip()}\n"
                f"  cmd: {' '.join(_redacted_command(cmd))}"
            )
            raise RuntimeError(message)
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
            message = "Container was not created before initialization"
            raise RuntimeError(message)

        # Save metadata for vibesys-shell to reconstruct the environment
        self._metadata: dict[str, object] = {
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
        if self._docker_in_docker:
            # Only a docker-in-docker sandbox records these; an ordinary
            # sandbox's metadata file stays exactly as it was.
            self._metadata["docker_in_docker"] = True
            self._metadata["container_root"] = self._container_root
        self._save_metadata()

        self._remap_agent_user(container_id)
        self._own_writable_mount_parents(container_id)
        self._copy_auth_files(container_id)
        if self._docker_in_docker:
            start_nested_daemon(self._docker, container_id)

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
            result = self._docker.run(cmd, timeout_seconds=_ROOT_SETUP_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            self._log_cmd(cmd, error=f"{what} timed out after {_ROOT_SETUP_TIMEOUT_S}s")
            message = f"{what} timed out after {_ROOT_SETUP_TIMEOUT_S}s"
            raise RuntimeError(message) from exc
        self._log_cmd(cmd, result)
        if result.returncode != 0:
            message = (
                f"{what} failed (exit {result.returncode}):\n"
                f"  stdout: {result.stdout.strip()[:500]}\n"
                f"  stderr: {result.stderr.strip()[:500]}"
            )
            raise RuntimeError(message)

    def _current_agent_ids(self, container_id: str) -> tuple[int, int] | None:
        """Return the image's ``agent`` user's current (uid, gid), if resolvable."""
        result = self._docker.run(
            [
                "docker",
                "exec",
                container_id,
                "sh",
                "-c",
                f"id -u {_AGENT_USER} && id -g {_AGENT_USER}",
            ],
            timeout_seconds=_ROOT_SETUP_TIMEOUT_S,
        )
        if result.returncode != 0:
            return None
        lines = result.stdout.split()
        if len(lines) != _CONTAINER_IDENTITY_LINE_COUNT:
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

    def _own_writable_mount_parents(self, container_id: str) -> None:
        """Give the agent the directories Docker created above writable mounts in HOME.

        Docker creates the missing parents of a mount destination as root. A
        CLI keeps its sessions and caches next to a credential file that was
        mounted writable into ``~/.codex``, and a root-owned ``~/.codex``
        would refuse them. Only the directories are chowned, never the mounted
        path: that is the host's file.
        """
        directories: list[str] = []
        for _, container_path, readonly in self._bind_mounts:
            if readonly or not Path(container_path).is_relative_to(AGENT_HOME):
                continue
            for parent in Path(container_path).parents:
                if parent == Path(AGENT_HOME):
                    break
                if str(parent) not in directories:
                    directories.append(str(parent))
        if directories:
            quoted = " ".join(shlex.quote(directory) for directory in directories)
            self._run_as_root(
                container_id,
                f"chown {_AGENT_USER}:{_AGENT_USER} {quoted}",
                what="writable mount parent ownership",
            )

    def _copy_auth_files(self, container_id: str) -> None:
        """Copy staged provider settings into the agent's writable HOME.

        Copying from the read-only staging mount into the agent's own
        writable layer, rather than mounting the destination itself, keeps
        session/history writes inside the disposable container.
        """
        # Every directory created on the way to the destination must belong to
        # the agent too: a CLI writes sessions and caches next to its auth
        # file, and a root-owned ``~/.codex`` would refuse them. The trailing
        # chown covers the whole path below HOME, not just the copied file,
        # but skips anything mounted from the host: a writable credential file
        # is the host's file, and its ownership is not ours to change.
        for source, destination in self._auth_files:
            top = _first_component_below(AGENT_HOME, destination)
            mounted = sorted(
                container_path
                for _, container_path, _ in self._bind_mounts
                if Path(container_path).is_relative_to(top)
            )
            skip = "".join(f" ! -path {shlex.quote(path)}" for path in mounted)
            script = (
                f"mkdir -p {shlex.quote(str(Path(destination).parent))} && "
                f"cp -a {shlex.quote(source)} {shlex.quote(destination)} && "
                f"find {shlex.quote(top)} -xdev{skip} "
                f"-exec chown -h {_AGENT_USER}:{_AGENT_USER} {{}} +"
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

    def _remove_container_named(self, name: str) -> None:
        """Best-effort ``docker rm -f`` of a container known only by its name."""
        cmd = ["docker", "rm", "-f", name]
        with suppress(Exception):
            result = self._docker.run(cmd, timeout_seconds=_NAMED_REMOVE_TIMEOUT_S)
            self._log_cmd(cmd, result)

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
                result = self._docker.run(cmd, timeout_seconds=timeout)
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
            except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-009067 [BLE001]; cleanup must record failures but not replace an earlier startup or shutdown error.
                if cmd[1] == "rm":
                    cleanup_error = exc
                with suppress(Exception):
                    self._log_cmd(cmd, error=f"container cleanup failed: {exc}")

        if not removed and cleanup_error is not None and not suppress_errors:
            raise cleanup_error
        return removed

    def _save_metadata(self) -> None:
        """Write metadata to the host workspace (best-effort)."""
        try:
            metadata_path = Path(self._host_workspace) / ".docker_metadata.json"
            atomic_write_bytes(metadata_path, json.dumps(self._metadata, indent=2).encode())
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

    def restart_with_gpus(self, gpus: str | None) -> None:
        """Restart the container with a new Docker GPU selection.

        A Sysbox sandbox never had an accelerator (it cannot forward one), so
        a reselection has nothing to move: restarting it would only discard
        the run's nested daemon state, and attaching *gpus* would break the
        runtime the constructor refuses to combine with them.
        """
        if self._docker_in_docker:
            return
        self.stop()
        self._gpus = gpus
        self.start()

    @property
    def container_id(self) -> str:
        """Return the Docker container id backing this sandbox.

        Callers that need to run their own ``docker`` commands against the
        container (a command executor, an exec probe) read the id here rather
        than the private attribute. Raises ``RuntimeError`` when no container
        is running, which is also the state after :meth:`stop`.
        """
        if self._container_id is None:
            raise DockerSandboxNotStartedError.container_id_unavailable(self._host_workspace)
        return self._container_id

    @property
    def id(self) -> str:
        """Return sandbox identifier."""
        if self._container_id:
            return self._container_name
        return "vibesys-not-started"

    def agent_path(self, host_path: Path | str) -> str:
        """Return the container path the agent sees for *host_path*.

        Consults the resource list this sandbox was built from — each
        resource's ``agent_path``, or its own host path when unset — plus the
        workspace's fixed mapping to ``/workspace``, matched by longest
        host-path prefix so a path nested inside a mapped resource maps too.
        Falls back to identity, normalised the same way the host backends'
        default implementation does, when nothing matches: a container-only
        path (already under ``/workspace`` or another mount) passed back in
        is unaffected.
        """
        normalized = str(Path(host_path))
        for host_prefix, container_prefix in self._agent_path_map:
            if normalized == host_prefix:
                return container_prefix
            if normalized.startswith(host_prefix + "/"):
                return container_prefix + normalized[len(host_prefix) :]
        return normalized

    @property
    def env(self) -> Mapping[str, str]:
        """Return the environment the container's agent user runs with.

        ``HOME`` is the image's fixed agent home. ``PATH`` is read once from
        the running container and cached for this sandbox's lifetime: the
        agent layer prepends toolchain directories onto whatever PATH the
        base image already set, so no constant is portable across the base
        images different backends choose. ``extra_env`` (the *env* this
        sandbox was constructed with) is applied last, so a caller's override
        wins over either.
        """
        if self._container_id is None:
            raise DockerSandboxNotStartedError.operation_before_start()
        if self._cached_container_path is None:
            self._cached_container_path = self._read_container_path(self._container_id)
        return {"HOME": AGENT_HOME, "PATH": self._cached_container_path, **self._env}

    def _read_container_path(self, container_id: str) -> str:
        """Read the running container's PATH via a one-shot ``docker exec``."""
        cmd = ["docker", "exec", container_id, "sh", "-c", "echo $PATH"]
        self._log_cmd(cmd)
        result = self._docker.run(cmd, timeout_seconds=_ROOT_SETUP_TIMEOUT_S)
        self._log_cmd(cmd, result)
        path = result.stdout.strip()
        if result.returncode != 0 or not path:
            message = (
                f"could not read PATH from container {container_id} "
                f"(exit {result.returncode}): {result.stderr.strip()}"
            )
            raise RuntimeError(message)
        return path

    def wrap(self, argv: list[str], cwd: Path | str | None = None) -> list[str]:
        """Return *argv* wrapped as a ``docker exec`` call into this container.

        ``-w`` is the agent path of *cwd*: unlike a host backend, whose
        ``wrap`` fixes the working directory to its own workspace internally,
        one Docker container serves every turn regardless of working
        directory, so the caller supplies *cwd* per call. Omitting *cwd*
        (matching the base ``WorkspaceSandbox.wrap(argv)`` signature) defaults
        to the workspace root, the same directory every other backend's
        ``wrap`` fixes unconditionally. Extra environment entries this sandbox
        was constructed with are forwarded as ``-e`` flags. The container
        already runs as the image's non-root default user (remapped to the
        host uid/gid at :meth:`start`), so no ``-u`` is emitted; this differs
        from :meth:`execute`, which always runs bash in ``/workspace`` for the
        framework's own filesystem operations rather than an agent turn's
        working directory.
        """
        if self._container_id is None:
            raise DockerSandboxNotStartedError.operation_before_start()
        workdir = self.agent_path(cwd) if cwd is not None else self._container_root
        env_flags = [flag for key, value in self._env.items() for flag in ("-e", f"{key}={value}")]
        return ["docker", "exec", "-i", "-w", workdir, *env_flags, self._container_id, *argv]

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        cancel: threading.Event | None = None,
    ) -> CommandResult:
        """Execute a command inside the Docker container.

        Every call tags its in-container processes with a per-call marker, so
        a timeout or a set *cancel* signals the whole process tree inside the
        container (``SIGTERM``, then ``SIGKILL`` after a grace period), not
        only the local ``docker exec`` client. The result contract is
        documented in :mod:`vs_sandbox.command_execution`; every sandbox kind
        shares it.
        """
        if self._container_id is None:
            raise DockerSandboxNotStartedError.operation_before_start()
        container_id = self._container_id
        exec_id = uuid.uuid4().hex
        exec_cmd = [
            "docker",
            "exec",
            "-e",
            f"{EXEC_MARKER_ENV}={exec_id}",
            "-w",
            self._container_root,
            container_id,
            "bash",
            "-c",
            command,
        ]
        self._log_cmd(exec_cmd)
        result = execute_command(
            command,
            timeout=timeout,
            default_timeout=self._default_timeout,
            cancel=cancel,
            max_output_chars=self._max_output_bytes,
            launch=lambda: self._docker.spawn(exec_cmd),
            signal_remote=lambda number: self._signal_exec(container_id, exec_id, number),
        )
        self._log_cmd(
            exec_cmd,
            subprocess.CompletedProcess(
                exec_cmd, result.exit_code or 0, result.stdout, result.stderr
            ),
        )
        return result

    def _signal_exec(self, container_id: str, exec_id: str, number: signal.Signals) -> None:
        """Signal every container process tagged with *exec_id*."""
        with suppress(OSError, subprocess.SubprocessError):
            self._docker.run(
                [
                    "docker",
                    "exec",
                    container_id,
                    "sh",
                    "-c",
                    SIGNAL_EXEC_SCRIPT,
                    "sh",
                    f"{EXEC_MARKER_ENV}={exec_id}",
                    str(number.value),
                ],
                timeout_seconds=_EXEC_STOP_TIMEOUT_S,
            )

    def __enter__(self) -> DockerSandbox:
        """Start the sandbox and return it as a context manager."""
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        """Stop the sandbox when leaving its context."""
        self.stop()
