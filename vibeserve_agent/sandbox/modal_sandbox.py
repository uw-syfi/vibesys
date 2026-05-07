"""Modal-based sandbox backend for running agent operations on remote GPUs.

Mirrors :class:`DockerSandbox` semantics using a persistent :class:`modal.Sandbox`
as the execution container.  Key differences:

- The workspace is backed by an **ephemeral Modal Volume** instead of a host
  bind mount.  Host -> container sync happens once at ``start()`` via
  ``Volume.batch_upload``; container -> host sync happens at ``stop()`` by
  walking the volume and pulling each file back.  In between, the host
  workspace directory does **not** reflect the agent's in-progress writes.
- Read-only model weights must live in a pre-existing named Modal Volume
  (pass the name via ``model_volume_name``).  The volume is mounted at
  ``/model`` with ``read_only=True``.
- Other RO bind mounts (``bench``, ``acc_checker``, ``nsys_profiler``) are
  uploaded once into the workspace volume under their container-path leaf.
"""

from __future__ import annotations

import atexit
import logging
import os
import shutil
import signal
import socket
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Callable, TypeVar

import modal
from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    WriteResult,
)
from deepagents.backends.sandbox import BaseSandbox

# Global registry of live sandboxes for cleanup on exit / SIGINT.
_live_sandboxes: dict[str, "ModalSandbox"] = {}

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Transient-error retry helper
# ---------------------------------------------------------------------------

# Substrings that indicate a transient network / control-plane error worth
# retrying rather than surfacing to the agent.  Sourced from real incidents:
# - "Name or service not known" — DNS failure (EAI_NONAME)
# - "Temporary failure in name resolution" — DNS failure (EAI_AGAIN)
# - gRPC transients Modal's client may raise when the control plane hiccups
_TRANSIENT_FRAGMENTS = (
    "name or service not known",
    "temporary failure in name resolution",
    "connection reset",
    "connection refused",
    "connection aborted",
    "broken pipe",
    "unavailable",           # grpc StatusCode.UNAVAILABLE
    "deadline_exceeded",     # grpc StatusCode.DEADLINE_EXCEEDED (network, not app timeout)
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "eof occurred",          # TLS handshake failures
    "handshake",
)

# Substrings that indicate the sandbox container itself is gone — these
# are NOT retried in place; the sandbox has to be recreated first.
# Sourced from Modal SDK error messages.
_SANDBOX_DEAD_FRAGMENTS = (
    "sandbox has already shut down",
    "sandbox has already terminated",
    "sandbox is not running",
    "sandbox not found",
    "sandbox has exited",
    "sandbox has been terminated",
)


def _is_sandbox_dead(exc: BaseException) -> bool:
    """Return True when the exception says the sandbox container is gone."""
    msg = str(exc).lower()
    return any(frag in msg for frag in _SANDBOX_DEAD_FRAGMENTS)


def _is_transient(exc: BaseException) -> bool:
    """Return True for errors we should retry (network/control-plane blips)."""
    # DNS-level: socket.gaierror is the canonical "cannot resolve hostname".
    if isinstance(exc, socket.gaierror):
        return True
    # ConnectionError subclasses (ConnectionRefusedError, ConnectionResetError, ...).
    if isinstance(exc, ConnectionError):
        return True
    msg = str(exc).lower()
    return any(frag in msg for frag in _TRANSIENT_FRAGMENTS)


def _retry_transient(
    fn: Callable[[], T],
    *,
    max_attempts: int = 4,
    base_delay: float = 2.0,
    log: Callable[[str], None] | None = None,
    label: str = "operation",
) -> T:
    """Run *fn*; retry on transient errors with exponential backoff.

    Raises the original exception if all attempts fail or if the exception
    isn't classified as transient.  Retries at 2s, 4s, 8s (default config
    caps total wait at ~14 s across 4 attempts).
    """
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except BaseException as exc:
            last_exc = exc
            if not _is_transient(exc) or attempt == max_attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            if log is not None:
                log(
                    f"transient {label} error (attempt {attempt}/{max_attempts}): "
                    f"{exc}; retrying in {delay:.1f}s"
                )
            time.sleep(delay)
    assert last_exc is not None  # unreachable
    raise last_exc


def _cleanup_sandboxes() -> None:
    """Terminate all tracked sandboxes and delete their ephemeral volumes."""
    for sb_id, sb in list(_live_sandboxes.items()):
        try:
            sb.stop()
        except Exception:
            pass
        _live_sandboxes.pop(sb_id, None)


atexit.register(_cleanup_sandboxes)

_original_sigint = signal.getsignal(signal.SIGINT)


def _sigint_handler(signum, frame):
    signal.signal(signal.SIGINT, _original_sigint)
    _cleanup_sandboxes()
    if callable(_original_sigint):
        _original_sigint(signum, frame)
    else:
        raise KeyboardInterrupt


signal.signal(signal.SIGINT, _sigint_handler)


class ModalSandbox(BaseSandbox):
    """Sandbox that runs agent operations inside a Modal Sandbox.

    The agent uses virtual absolute paths (``/foo``) expecting ``/`` to be
    the workspace root — matching ``LocalShellBackend(virtual_mode=True)``
    behaviour.  Paths are translated to ``/workspace/foo`` before dispatch.
    """

    _CONTAINER_ROOT = "/workspace"

    def __init__(
        self,
        host_workspace: str,
        image: str,
        gpu: str | None = "H100",
        default_timeout: int = 300,
        sandbox_timeout: int = 14400,
        idle_timeout: int | None = 1800,
        max_output_bytes: int = 100_000,
        env: dict[str, str] | None = None,
        bind_mounts: list[tuple[str, str, bool]] | None = None,
        passthrough_paths: list[str] | None = None,
        model_volume_name: str | None = None,
        extra_readonly_volumes: dict[str, str] | None = None,
        extra_writable_volumes: dict[str, str] | None = None,
        log_path: str | Path | None = None,
        extra_init_commands: list[str] | None = None,
        setup_fns: list["Callable[[ModalSandbox], None]"] | None = None,
        app_name: str = "vibeserve",
        enable_fallback_restart: bool = True,
        max_restart_attempts: int = 2,
    ) -> None:
        """Initialize the Modal sandbox configuration.

        Args:
            host_workspace: Host path whose contents are uploaded to the
                workspace volume and (at ``stop()``) downloaded back.
            image: Container image tag (must be pullable from the registry
                Modal is allowed to pull from).
            gpu: Modal GPU spec (e.g. ``"H100"``, ``"A100"``, ``"L40S"``).
            default_timeout: Per-``execute()`` timeout in seconds when the
                caller does not specify one.
            sandbox_timeout: Max lifetime of the sandbox in seconds.  Modal
                caps this at 24h.
            idle_timeout: Auto-terminate if no activity for this many
                seconds.  None disables the idle check.
            max_output_bytes: Truncate combined stdout+stderr beyond this
                length (matches DockerSandbox semantics).
            env: Environment variables to set in the sandbox.
            bind_mounts: List of ``(host_path, container_path, readonly)``
                tuples.  Paths under ``/workspace`` are uploaded to the
                workspace volume.  Paths under ``/model`` are ignored — use
                ``model_volume_name`` instead.
            passthrough_paths: Container paths outside ``/workspace`` that
                should not be rewritten by virtual-path translation.
            model_volume_name: Name of a pre-existing Modal Volume holding
                model weights; mounted read-only at ``/model``.
            extra_readonly_volumes: Additional ``mountpoint -> volume_name``
                entries, each mounted read-only at ``mountpoint``.  Used
                for auxiliary weights like EAGLE draft models
                (``/draft_model``).  Volumes are looked up lazily and must
                exist in Modal; populate them via
                ``vibeserve_agent.sandbox.modal_model_setup.ensure_model_volume``
                before starting the sandbox.
            extra_writable_volumes: Additional ``mountpoint -> volume_name``
                entries mounted read-write. Used for persistent CLI auth
                state such as Codex ChatGPT login inside Modal.
            log_path: Optional file for Modal API call logging.
            extra_init_commands: Additional bash one-liners run inside the
                sandbox after the default ``pip install uv``.  Failures
                raise ``RuntimeError``.
            app_name: Modal App name to attach the sandbox to.  Created if
                missing.
        """
        self._host_workspace = Path(host_workspace)
        self._image_tag = image
        self._gpu = gpu
        self._default_timeout = default_timeout
        self._sandbox_timeout = sandbox_timeout
        self._idle_timeout = idle_timeout
        self._max_output_bytes = max_output_bytes
        self._env = dict(env or {})
        self._bind_mounts = list(bind_mounts or [])
        self._passthrough_prefixes = list(passthrough_paths or [])
        self._model_volume_name = model_volume_name
        self._extra_readonly_volumes = dict(extra_readonly_volumes or {})
        self._extra_writable_volumes = dict(extra_writable_volumes or {})
        self._extra_init_commands = list(extra_init_commands or [])
        self._setup_fns = list(setup_fns or [])
        self._app_name = app_name
        self._enable_fallback_restart = enable_fallback_restart
        self._max_restart_attempts = max_restart_attempts
        self._restart_attempts = 0

        self._sandbox: modal.Sandbox | None = None
        self._sandbox_id: str | None = None
        self._workspace_volume: modal.Volume | None = None
        self._workspace_volume_name: str | None = None
        self._logger = self._setup_logger(log_path)

    @staticmethod
    def _setup_logger(log_path: str | Path | None) -> logging.Logger | None:
        if log_path is None:
            return None
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger(f"modal_sandbox.{uuid.uuid4().hex[:8]}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        handler = logging.FileHandler(str(log_path))
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        return logger

    def _log(self, msg: str) -> None:
        if self._logger is not None:
            self._logger.info(msg)

    # -- virtual-path translation (mirrors DockerSandbox) ------------------

    def _vpath(self, path: str) -> str:
        if path.startswith(self._CONTAINER_ROOT + "/") or path == self._CONTAINER_ROOT:
            return path
        for prefix in self._passthrough_prefixes:
            if path == prefix or path.startswith(prefix + "/"):
                return path
        if path.startswith("/"):
            return self._CONTAINER_ROOT + path
        return path

    def ls_info(self, path: str):
        return super().ls_info(self._vpath(path))

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> str:
        return super().read(self._vpath(file_path), offset, limit)

    def edit(self, file_path: str, old_string: str, new_string: str,
             replace_all: bool = False):
        return super().edit(self._vpath(file_path), old_string, new_string, replace_all)

    def glob_info(self, pattern: str, path: str = "/"):
        return super().glob_info(pattern, self._vpath(path))

    def grep_raw(self, pattern: str, path: str | None = None,
                 glob: str | None = None):
        return super().grep_raw(
            pattern,
            self._vpath(path) if path is not None else self._CONTAINER_ROOT,
            glob,
        )

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Create the Modal sandbox, upload workspace, start the container."""
        # Ephemeral per-run workspace volume.  Created once; reused on
        # restart so in-progress agent state survives a sandbox crash.
        self._workspace_volume_name = f"vibeserve-ws-{uuid.uuid4().hex[:12]}"
        self._workspace_volume = modal.Volume.from_name(
            self._workspace_volume_name, create_if_missing=True,
        )
        self._log(f"created workspace volume {self._workspace_volume_name}")

        self._populate_workspace_volume()
        self._create_container()

    def _create_container(self) -> None:
        """Create (or recreate) the Modal sandbox container on top of the
        already-populated workspace volume. Shared by ``start`` and
        ``_restart_sandbox``."""
        app = modal.App.lookup(self._app_name, create_if_missing=True)
        # Clear the image's /workspace dir so our Volume can mount there —
        # the nvcr.io pytorch image ships with a populated /workspace that
        # triggers init_failure when a Volume is mounted on top.
        image = modal.Image.from_registry(
            self._image_tag, add_python=None,
        ).run_commands(f"rm -rf {self._CONTAINER_ROOT} && mkdir {self._CONTAINER_ROOT}")

        volumes: dict[str, modal.Volume] = {self._CONTAINER_ROOT: self._workspace_volume}
        if self._model_volume_name:
            model_vol = modal.Volume.from_name(self._model_volume_name).read_only()
            volumes["/model"] = model_vol
            self._log(f"mounting model volume {self._model_volume_name} at /model")
        for mountpoint, vol_name in self._extra_readonly_volumes.items():
            aux_vol = modal.Volume.from_name(vol_name).read_only()
            volumes[mountpoint] = aux_vol
            self._log(f"mounting aux volume {vol_name} at {mountpoint}")
        for mountpoint, vol_name in self._extra_writable_volumes.items():
            extra_vol = modal.Volume.from_name(vol_name, create_if_missing=True)
            volumes[mountpoint] = extra_vol
            self._log(f"mounting writable volume {vol_name} at {mountpoint}")

        self._log(
            f"create sandbox image={self._image_tag} gpu={self._gpu} "
            f"timeout={self._sandbox_timeout} idle_timeout={self._idle_timeout}"
        )
        # Pass an explicit long-running command so the sandbox stays alive
        # between exec() calls. Without this, the container's default CMD
        # runs and exits, and subsequent execs hit "Sandbox has already
        # shut down". Mirrors DockerSandbox's `sleep infinity` trick.
        self._sandbox = modal.Sandbox.create(
            "sleep", "infinity",
            app=app,
            image=image,
            gpu=self._gpu,
            timeout=self._sandbox_timeout,
            idle_timeout=self._idle_timeout,
            workdir=self._CONTAINER_ROOT,
            volumes=volumes,
            env=self._env or None,
        )
        self._sandbox_id = self._sandbox.object_id
        _live_sandboxes[self._sandbox_id] = self
        self._log(f"sandbox created id={self._sandbox_id}")

        # Install uv (non-fatal; mirrors DockerSandbox).
        try:
            proc = self._sandbox.exec(
                "bash", "-c", "pip install uv",
                workdir=self._CONTAINER_ROOT, timeout=180,
            )
            proc.wait()
        except Exception as exc:
            self._log(f"pip install uv failed: {exc}")

        # Run required init commands — fatal on failure.
        for cmd in self._extra_init_commands:
            self._log(f"init command: {cmd}")
            proc = self._sandbox.exec(
                "bash", "-c", cmd,
                workdir=self._CONTAINER_ROOT, timeout=600,
            )
            code = proc.wait()
            if code != 0:
                try:
                    err = proc.stderr.read() if proc.stderr else ""
                except Exception:
                    err = ""
                raise RuntimeError(
                    f"Modal sandbox init command failed (exit {code}):\n"
                    f"  command: {cmd}\n"
                    f"  stderr: {err[:500]}"
                )
            if "codex login status" in cmd:
                try:
                    out = proc.stdout.read() if proc.stdout else ""
                except Exception:
                    out = ""
                if out:
                    self._log(f"init command stdout: {out.strip()[:500]}")
            self._log(f"init command completed: {cmd}")

        # Run caller-supplied setup functions.  These re-execute on every
        # restart so transient in-container state (symlinks etc.) survives.
        for fn in self._setup_fns:
            fn(self)

    def _restart_sandbox(self) -> bool:
        """Terminate the (likely dead) sandbox container and recreate it.

        The workspace volume is preserved, so files the agent wrote before
        the crash survive.  Returns True if recreation succeeded.  Honours
        ``_max_restart_attempts`` to avoid infinite restart loops.
        """
        if not self._enable_fallback_restart:
            return False
        if self._restart_attempts >= self._max_restart_attempts:
            self._log(
                f"[fallback] not restarting: already restarted "
                f"{self._restart_attempts} time(s) (max "
                f"{self._max_restart_attempts})"
            )
            return False
        self._restart_attempts += 1
        self._log(
            f"[fallback] sandbox appears dead — recreating "
            f"(attempt {self._restart_attempts}/{self._max_restart_attempts})"
        )
        old_id = self._sandbox_id
        try:
            if self._sandbox is not None:
                try:
                    self._sandbox.terminate()
                except Exception:
                    pass
        finally:
            if old_id is not None:
                _live_sandboxes.pop(old_id, None)
            self._sandbox = None
            self._sandbox_id = None

        try:
            self._create_container()
            self._log(
                f"[fallback] restart succeeded (new id={self._sandbox_id})"
            )
            return True
        except Exception as exc:
            self._log(f"[fallback] restart failed: {exc}")
            return False

    def _populate_workspace_volume(self) -> None:
        """Upload host workspace + RO bind mount contents to the workspace volume."""
        if self._workspace_volume is None:
            return
        vol = self._workspace_volume
        tempdirs: list[tempfile.TemporaryDirectory[str]] = []
        try:
            with vol.batch_upload(force=True) as batch:
                if self._host_workspace.exists() and self._host_workspace.is_dir():
                    self._put_workspace_directory(batch, self._host_workspace, "/")
                for host_path, container_path, _readonly in self._bind_mounts:
                    # /model is not backed by the workspace volume.
                    if container_path == "/model" or container_path.startswith("/model/"):
                        continue
                    # Only mounts inside /workspace/* can be served from this volume.
                    if not container_path.startswith(self._CONTAINER_ROOT):
                        self._log(
                            f"skip bind mount outside {self._CONTAINER_ROOT}: {container_path}"
                        )
                        continue
                    rel = container_path[len(self._CONTAINER_ROOT):].lstrip("/") or "/"
                    host_p = Path(host_path)
                    remote = "/" + rel if rel != "/" else "/"
                    if ".hf_cache" in host_p.parts:
                        self._log(f"skip local Hugging Face cache upload: {host_p}")
                        continue
                    if host_p.is_dir():
                        self._put_bind_mount_directory(batch, host_p, remote, tempdirs)
                    elif host_p.is_file():
                        batch.put_file(str(host_p), remote)
        finally:
            for td in tempdirs:
                td.cleanup()

    _UPLOAD_EXCLUDES = frozenset(
        {
            ".git",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".venv",
            "__pycache__",
            "artifacts",
            "exp_env",
        }
    )

    def _put_workspace_directory(self, batch: object, local_root: Path, remote_root: str) -> None:
        """Upload workspace files while skipping local-only runtime trees."""
        remote_base = PurePosixPath(remote_root)
        for path in local_root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(local_root)
            if any(part in self._UPLOAD_EXCLUDES for part in rel.parts):
                continue
            batch.put_file(str(path), str(remote_base / PurePosixPath(*rel.parts)))

    def _put_bind_mount_directory(
        self,
        batch: object,
        local_root: Path,
        remote_root: str,
        tempdirs: list[tempfile.TemporaryDirectory[str]],
    ) -> None:
        """Upload a stable snapshot of a bind-mounted directory."""
        tmp = tempfile.TemporaryDirectory(prefix="vibeserve-modal-upload-")
        tempdirs.append(tmp)
        snapshot = Path(tmp.name) / local_root.name
        self._copy_bind_mount_snapshot(local_root, snapshot)
        batch.put_directory(str(snapshot), remote_root)

    _CODEX_AUTH_FILES = frozenset(
        {
            "auth.json",
            "config.toml",
            "installation_id",
            "version.json",
        }
    )

    def _copy_bind_mount_snapshot(self, local_root: Path, snapshot: Path) -> None:
        """Copy a bind mount to a stable upload snapshot."""
        if local_root.name == ".codex":
            snapshot.mkdir(parents=True, exist_ok=True)
            for name in self._CODEX_AUTH_FILES:
                src = local_root / name
                if src.is_file():
                    shutil.copy2(src, snapshot / name)
            for src in local_root.glob("state_*.sqlite*"):
                if src.is_file():
                    shutil.copy2(src, snapshot / src.name)
            rules = local_root / "rules"
            if rules.is_dir():
                shutil.copytree(rules, snapshot / "rules", symlinks=True)
            return
        shutil.copytree(local_root, snapshot, symlinks=True)

    @property
    def id(self) -> str:
        return self._sandbox_id or "modal-not-started"

    # -- shared fallback wrapper ------------------------------------------

    def _run_with_fallback(self, fn: Callable[[], T], *, label: str) -> T:
        """Run *fn* with transient-retry; recreate sandbox on death and retry.

        Layered semantics:

        1. Network/control-plane hiccups → ``_retry_transient`` handles it.
        2. ``Sandbox has already shut down`` / similar → terminate the dead
           sandbox, recreate a fresh container on top of the existing
           workspace volume, retry *fn* once.
        3. Anything else → let the exception propagate so callers can
           render the error.
        """
        try:
            return _retry_transient(fn, log=self._log, label=label)
        except Exception as exc:
            if _is_sandbox_dead(exc) and self._restart_sandbox():
                return _retry_transient(
                    fn, log=self._log, label=f"{label}-after-restart",
                )
            raise

    # -- execute -----------------------------------------------------------

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        if self._sandbox is None:
            raise RuntimeError("Sandbox not started — call start() first")

        effective_timeout = timeout if timeout is not None else self._default_timeout
        self._log(f"exec (timeout={effective_timeout}s): {command[:500]}")

        def _do_exec() -> tuple[str, str, int]:
            proc = self._sandbox.exec(
                "bash", "-c", command,
                workdir=self._CONTAINER_ROOT,
                timeout=effective_timeout,
            )
            stdout = proc.stdout.read() if proc.stdout else ""
            stderr = proc.stderr.read() if proc.stderr else ""
            exit_code = proc.wait()
            return stdout, stderr, exit_code

        try:
            stdout, stderr, exit_code = self._run_with_fallback(
                _do_exec, label="exec",
            )
        except TimeoutError:
            self._log(f"exec timeout after {effective_timeout}s")
            return ExecuteResponse(
                output=f"Command timed out after {effective_timeout}s",
                exit_code=-1,
                truncated=False,
            )
        except Exception as exc:
            self._log(f"exec error: {exc}")
            return ExecuteResponse(
                output=f"Modal exec error: {exc}",
                exit_code=-1,
                truncated=False,
            )

        output = (stdout or "") + (stderr or "")
        truncated = False
        if len(output) > self._max_output_bytes:
            total = len(output)
            output = (
                output[: self._max_output_bytes]
                + f"\n... [truncated, {total - self._max_output_bytes} bytes omitted]"
            )
            truncated = True
        return ExecuteResponse(
            output=output,
            exit_code=exit_code,
            truncated=truncated,
        )

    # -- file ops ----------------------------------------------------------

    def write(self, file_path: str, content: str) -> WriteResult:
        """Override BaseSandbox.write to avoid ARG_MAX limits on large files."""
        if self._sandbox is None:
            raise RuntimeError("Sandbox not started — call start() first")
        container_path = self._vpath(file_path)
        parent = str(Path(container_path).parent)

        def _do_write() -> None:
            fs = self._sandbox.filesystem
            try:
                fs.make_directory(parent, parents=True)
            except TypeError:
                # Older Modal SDKs: make_directory may not accept parents=
                self._sandbox.exec(
                    "bash", "-c", f"mkdir -p {parent}",
                    workdir=self._CONTAINER_ROOT,
                ).wait()
            fs.write_text(content, container_path)

        try:
            self._run_with_fallback(_do_write, label=f"write {file_path}")
        except Exception as exc:
            self._log(f"write {file_path} failed: {exc}")
            return WriteResult(path=file_path, error=str(exc))
        return WriteResult(path=file_path)

    def upload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        if self._sandbox is None:
            raise RuntimeError("Sandbox not started — call start() first")
        results: list[FileUploadResponse] = []
        for path, content in files:
            container_path = self._vpath(path)
            parent = str(Path(container_path).parent)

            def _do_upload(
                _parent: str = parent,
                _path: str = container_path,
                _content: bytes = content,
            ) -> None:
                # Re-read fs on each call so post-restart we get the new
                # sandbox's filesystem handle, not a stale reference.
                fs = self._sandbox.filesystem
                self._sandbox.exec(
                    "bash", "-c", f"mkdir -p {_parent}",
                    workdir=self._CONTAINER_ROOT,
                ).wait()
                fs.write_bytes(_content, _path)

            try:
                self._run_with_fallback(_do_upload, label=f"upload {path}")
                results.append(FileUploadResponse(path=path))
            except Exception as exc:
                self._log(f"upload {path} failed: {exc}")
                results.append(FileUploadResponse(path=path, error="permission_denied"))
        return results

    def download_files(
        self,
        paths: list[str],
    ) -> list[FileDownloadResponse]:
        if self._sandbox is None:
            raise RuntimeError("Sandbox not started — call start() first")
        results: list[FileDownloadResponse] = []
        for path in paths:
            container_path = self._vpath(path)
            try:
                content = self._run_with_fallback(
                    lambda p=container_path: self._sandbox.filesystem.read_bytes(p),
                    label=f"download {path}",
                )
                results.append(FileDownloadResponse(path=path, content=content))
            except Exception as exc:
                self._log(f"download {path} failed: {exc}")
                results.append(FileDownloadResponse(path=path, error="file_not_found"))
        return results

    # -- compat shims for vibeserve-shell metadata (DockerSandbox has these) --

    def save_symlink_commands(self, symlink_commands: list[str]) -> None:
        """No-op: vibeserve-shell reattachment isn't supported for Modal yet."""
        return

    # -- shutdown ----------------------------------------------------------

    def stop(self) -> None:
        """Terminate the sandbox and sync the workspace back to the host."""
        if self._sandbox is None:
            return

        # Download BEFORE clearing self._sandbox — _download_workspace
        # early-returns when self._sandbox is None.
        try:
            self._download_workspace()
        except Exception as exc:
            self._log(f"workspace download failed: {exc}")

        sandbox = self._sandbox
        sandbox_id = self._sandbox_id
        self._sandbox = None
        self._sandbox_id = None
        if sandbox_id is not None:
            _live_sandboxes.pop(sandbox_id, None)

        try:
            sandbox.terminate()
        except Exception as exc:
            self._log(f"terminate failed: {exc}")

        if self._workspace_volume_name:
            try:
                modal.Volume.objects.delete(
                    self._workspace_volume_name, allow_missing=True,
                )
                self._log(f"deleted workspace volume {self._workspace_volume_name}")
            except Exception as exc:
                self._log(f"volume delete failed: {exc}")
            self._workspace_volume_name = None
            self._workspace_volume = None

    # Dirs inside /workspace that we don't download back to the host —
    # they're either huge (.venv), caches (__pycache__), or already-known
    # source dirs that the host already has (the bind-mount uploads).
    _DOWNLOAD_EXCLUDES = (
        ".venv",
        "__pycache__",
        ".pytest_cache",
        "_mounts",
        "_auth",            # host CLI auth uploaded from ~/.codex etc.
        "_opt_vibeserve",   # vibeserve_agent pkg uploaded for MCP
        "acc_checker",
        "bench",
        "skills",
        "nsys_profiler",
        "reference",
        ".git",
    )

    def _download_workspace(self) -> None:
        """Pull the remote workspace back to the host.

        Creates a tarball inside the sandbox (excluding large/redundant
        dirs), streams it back in one round-trip, and extracts.  Per-file
        downloads over the Volume API are too slow for workspaces with
        hundreds of files.
        """
        if self._sandbox is None:
            return
        if not self._host_workspace.exists():
            self._host_workspace.mkdir(parents=True, exist_ok=True)

        import tarfile
        import tempfile

        exclude_args = " ".join(
            f"--exclude='{e}'" for e in self._DOWNLOAD_EXCLUDES
        )
        tar_remote = "/tmp/_vibeserve_ws.tar.gz"
        tar_cmd = (
            f"cd {self._CONTAINER_ROOT} && "
            f"tar {exclude_args} -czf {tar_remote} . 2>/dev/null && "
            f"stat -c '%s' {tar_remote}"
        )
        try:
            proc = self._sandbox.exec(
                "bash", "-c", tar_cmd,
                workdir=self._CONTAINER_ROOT,
                timeout=300,
            )
            size_out = proc.stdout.read() if proc.stdout else ""
            if proc.wait() != 0:
                self._log("workspace tar creation failed")
                return
            self._log(f"workspace tarball size: {size_out.strip()} bytes")

            data = _retry_transient(
                lambda: self._sandbox.filesystem.read_bytes(tar_remote),
                log=self._log, label="workspace tar download",
            )
        except Exception as exc:
            self._log(f"workspace tar download failed: {exc}")
            return

        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            with tarfile.open(tmp_path, "r:gz") as tar:
                # Safe extraction: resolve each member against host_workspace
                # and skip anything that would escape.
                host_root = self._host_workspace.resolve()
                for member in tar.getmembers():
                    member_path = (host_root / member.name).resolve()
                    try:
                        member_path.relative_to(host_root)
                    except ValueError:
                        self._log(f"skipping unsafe path in tar: {member.name}")
                        continue
                    tar.extract(member, str(host_root), set_attrs=False)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> "ModalSandbox":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
