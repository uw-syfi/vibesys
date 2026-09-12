"""Run a trusted evaluator against a candidate deployed on Modal.

The Modal run environment edits candidates in a local CPU-only container, so
service-style evaluators cannot reach the serving process at their default
``http://localhost:8000``. This helper is mounted read-only by the runtime
adapter. It deploys the current candidate, discovers the emitted web endpoint,
waits for readiness, and then runs the trusted command *unmodified inside the
serving container* (via ``modal container exec``) so the task's localhost
measurement contract holds: metrics reflect the engine, not TLS, WAN, Modal
ingress, or Modal's function-admission queue.

The public endpoint is used only for deploy discovery, readiness, and periodic
keep-warm probes during the in-container run (colocated traffic does not reset
Modal's idle scaledown timer). Workspace-relative input files referenced by the
command and an explicitly selected framework-owned evaluator package are staged
into the container. An optional framework-owned setup argv runs from that random
stage before the evaluator. Absolute not-yet-existing paths in the command (for
example a ``--output-json`` target) are relayed back to the caller after the run.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import io
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Generator, Sequence  # noqa: TC003  # tracked: #288
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_MODAL_WEB_URL = re.compile(r"https://[a-zA-Z0-9.-]+\.modal\.run")
_MODAL_DEPLOYMENT = re.compile(
    r"https://modal\.com/apps/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+/deployed/"
    r"([a-zA-Z0-9_.-]+)"
)
_CANDIDATE_REVISION_ENV = "VIBESYS_CANDIDATE_REVISION"
_RELEASE_DEPLOYMENT_ENV = "VIBESYS_RELEASE_MODAL_DEPLOYMENT"
_MAX_DIAGNOSTIC_CHARS = 20_000
_EXEC_RC_MARKER = "__VIBESYS_EXEC_RC__="
_OUTPUT_FILE_MARKER = "__VIBESYS_OUTPUT_FILE__"
_OUTPUT_END_MARKER = "__VIBESYS_OUTPUT_END__"
_TRUSTED_SETUP_FAILURE_EXIT_CODE = 96
_EVALUATOR_BOOTSTRAP_FAILURE_EXIT_CODE = 97
_EVALUATOR_PACKAGE_STAGE_PATH = ".vibesys-evaluator-package"
_EVALUATOR_TOOLS_STAGE_PATH = ".vibesys-evaluator-tools"
_EVALUATOR_TOOLCHAINS_STAGE_PATH = ".vibesys-evaluator-toolchains"
_FRAMEWORK_BIN_STAGE_PATH = ".bin"
_FRAMEWORK_PIP_STAGE_PATH = ".pip"
_FRAMEWORK_UV_CACHE_STAGE_PATH = ".uv-cache"
# Linux caps a single argv string at 128 KiB; stay well under it per chunk.
_B64_CHUNK_CHARS = 60_000
_MAX_ENCODED_SETUP_COMMAND_CHARS = 60_000
_MAX_STAGE_ARCHIVE_BYTES = 8 * 1024 * 1024
_CONTAINER_DISCOVERY_TIMEOUT_SECONDS = 90.0
_KEEPWARM_INTERVAL_SECONDS = 30.0


def _runtime_dir() -> Path:
    """Return this process's private per-user runtime directory.

    Prefers ``XDG_RUNTIME_DIR``, which is already private per user by POSIX
    convention; otherwise falls back to a uid-suffixed directory under the
    system temp directory so unrelated users sharing a host cannot collide
    on the evaluator's lock or lease files.
    """
    xdg_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime_dir:
        return Path(xdg_runtime_dir) / "vibesys"
    return Path(tempfile.gettempdir()) / f"vibesys-{os.getuid()}"


_LOCK_PATH = _runtime_dir() / "modal-evaluator.lock"
_DEPLOYMENT_LEASE_PATH = _runtime_dir() / "modal-evaluator-deployment.json"


def _ensure_runtime_dir(path: Path) -> None:
    """Create and validate the private per-user runtime directory for ``path``.

    Raises ``RuntimeError`` naming the directory and its owning uid when the
    location cannot be used as a private per-user directory, for example a
    plain file already occupies it, another user owns it, or it already exists
    writable by other users, so a hostile or stale runtime-directory entry
    fails with a clear message instead of a deep ``PermissionError`` surfacing
    from ``flock`` or ``open``.
    """
    runtime_dir = path.parent
    try:
        runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = runtime_dir.lstat()
    except OSError as exc:
        raise RuntimeError(  # noqa: TRY003
            f"cannot use {runtime_dir} as the evaluator runtime directory: {exc}"
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise RuntimeError(  # noqa: TRY003
            f"refusing to use {runtime_dir} as the evaluator runtime directory: "
            f"expected a directory owned by uid {os.getuid()}"
        )
    # ``mkdir(exist_ok=True)`` accepts a directory that already existed, and the
    # ``chmod`` below only closes access from here on: it cannot remove files
    # another user planted while the directory was writable by them. So reject a
    # writable directory instead of coercing it. Test the write bits alone, not
    # all of ``0o077``, because write permission is the planting vector while a
    # readable ``0o755`` directory is both safe and common.
    if metadata.st_mode & 0o022:
        raise RuntimeError(  # noqa: TRY003
            f"refusing to use {runtime_dir} as the evaluator runtime directory: "
            "it is group- or world-writable, so another user may already have "
            "planted files in it"
        )
    runtime_dir.chmod(0o700)


@dataclass(frozen=True)
class _DeploymentLease:
    candidate_revision: str
    base_url: str
    app_identifier: str | None = None


def _normalized_setup_command(command: Sequence[str]) -> tuple[str, ...]:
    """Snapshot one opaque executable argv or reject malformed input."""
    if isinstance(command, str) or any(not isinstance(item, str) for item in command):
        raise TypeError("trusted setup command must contain only argv strings")  # noqa: TRY003
    if not command:
        raise ValueError("trusted setup command must be a non-empty string argv")  # noqa: TRY003
    normalized = tuple(command)
    if not normalized[0]:
        raise ValueError("trusted setup command executable must not be empty")  # noqa: TRY003
    return normalized


def encode_setup_command(command: Sequence[str]) -> str:
    """Encode trusted setup argv for ``--setup-command-base64``."""
    normalized = _normalized_setup_command(command)
    document = json.dumps(normalized, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(document).decode("ascii")
    if len(encoded) > _MAX_ENCODED_SETUP_COMMAND_CHARS:
        raise ValueError("trusted setup command exceeds the encoded size limit")  # noqa: TRY003
    return encoded


def _decode_setup_command(encoded: str) -> tuple[str, ...]:
    """Decode and structurally validate framework-owned setup argv."""
    if len(encoded) > _MAX_ENCODED_SETUP_COMMAND_CHARS:
        raise ValueError("trusted setup command exceeds the encoded size limit")  # noqa: TRY003
    try:
        payload = base64.b64decode(encoded, altchars=b"-_", validate=True)
        document = json.loads(payload.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("trusted setup command is not valid base64-encoded JSON argv") from exc  # noqa: TRY003
    if not isinstance(document, list):
        raise TypeError("trusted setup command must decode to a JSON argv array")  # noqa: TRY003
    return _normalized_setup_command(document)


def _setup_command_argument(encoded: str) -> tuple[str, ...]:
    """Translate setup-command validation into an argparse diagnostic."""
    try:
        return _decode_setup_command(encoded)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _compact_rich_output(output: str) -> str:
    """Remove terminal formatting that Rich can insert inside wrapped URLs."""
    plain = _ANSI_ESCAPE.sub("", output)
    # Modal renders deployment summaries in a Rich tree. Long URLs can wrap
    # onto the next line after the tree's vertical guide, for example
    # ``.moda\n│   l.run``. Whitespace removal alone leaves the guide inside the
    # URL, so discard Unicode box-drawing characters as well.
    return re.sub(r"[\s\u2500-\u257f]+", "", plain)


@contextmanager
def _exclusive_evaluation() -> Generator[None]:
    """Serialize deploy-and-evaluate callers sharing the editor container."""
    _ensure_runtime_dir(_LOCK_PATH)
    # ``O_NOFOLLOW`` so a symlink planted at the lock path fails instead of
    # redirecting this truncating write outside the runtime directory.
    lock_fd = os.open(_LOCK_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def extract_modal_web_url(output: str) -> str:
    """Return the last Modal web endpoint printed by ``modal deploy``."""
    # Rich may wrap a long endpoint between ``moda`` and ``l.run``. Removing
    # terminal layout is safe before matching because Modal hostnames contain
    # neither whitespace nor box-drawing characters.
    compact = _compact_rich_output(output)
    matches = _MODAL_WEB_URL.findall(compact)
    if not matches:
        raise ValueError("modal deploy did not print a *.modal.run web endpoint")  # noqa: TRY003  # tracked: #288
    return matches[-1]


def extract_modal_app_identifier(output: str) -> str:
    """Return the deployed Modal app name printed by ``modal deploy``."""
    compact = _compact_rich_output(output)
    matches = _MODAL_DEPLOYMENT.findall(compact)
    if not matches:
        raise ValueError("modal deploy did not print a deployment URL")  # noqa: TRY003  # tracked: #288
    return matches[-1]


def recent_modal_logs(app_identifier: str, *, workspace: str) -> str:
    """Fetch a bounded recent-log excerpt for a failed readiness check."""
    try:
        result = subprocess.run(  # noqa: S603  # tracked: #288
            [  # noqa: S607  # tracked: #288
                "uv",
                "run",
                "modal",
                "app",
                "logs",
                app_identifier,
                "--since",
                "10m",
                "--tail",
                "200",
                "--timestamps",
            ],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Could not fetch Modal logs: {type(exc).__name__}: {exc}"

    output = _ANSI_ESCAPE.sub("", f"{result.stdout}\n{result.stderr}".strip())
    if not output:
        return "Modal returned no recent logs."
    if len(output) > _MAX_DIAGNOSTIC_CHARS:
        output = f"[... earlier Modal logs omitted ...]\n{output[-_MAX_DIAGNOSTIC_CHARS:]}"
    return output


def wait_for_health(base_url: str, *, timeout_seconds: float) -> None:
    """Wait until the deployed candidate returns HTTP 200 from ``/health``."""
    deadline = time.monotonic() + timeout_seconds
    health_url = f"{base_url.rstrip('/')}/health"
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=10) as response:  # noqa: S310  # tracked: #288
                if response.status == 200:  # noqa: PLR2004  # tracked: #288
                    return
                last_error = f"HTTP {response.status}"
        except (OSError, urllib.error.URLError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(2)
    raise TimeoutError(f"{health_url} did not become ready: {last_error}")  # noqa: TRY003  # tracked: #288


def _healthy_now(base_url: str) -> bool:
    """Return whether an existing deployment is immediately reusable."""
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/health", timeout=5) as response:  # noqa: S310  # tracked: #288
            return response.status == 200  # noqa: PLR2004  # tracked: #288
    except (OSError, urllib.error.URLError):
        return False


def _read_deployment_lease() -> _DeploymentLease | None:
    try:
        # ``O_NOFOLLOW`` so a symlink planted at the lease path is ignored
        # rather than followed into a file outside the runtime directory.
        lease_fd = os.open(_DEPLOYMENT_LEASE_PATH, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(lease_fd) as lease_file:
            payload = json.load(lease_file)
        revision = payload["candidate_revision"]
        base_url = payload["base_url"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(revision, str) or not isinstance(base_url, str):
        return None
    app_identifier = payload.get("app_identifier")
    if app_identifier is not None and not isinstance(app_identifier, str):
        return None
    return _DeploymentLease(revision, base_url, app_identifier)


def _write_deployment_lease(
    candidate_revision: str,
    base_url: str,
    app_identifier: str | None,
) -> None:
    _ensure_runtime_dir(_DEPLOYMENT_LEASE_PATH)
    temporary = _DEPLOYMENT_LEASE_PATH.with_suffix(".tmp")
    payload = {
        "candidate_revision": candidate_revision,
        "base_url": base_url,
    }
    if app_identifier is not None:
        payload["app_identifier"] = app_identifier
    # The rename below replaces a symlink at the lease path rather than
    # following it, but this staging write would follow one planted at the
    # sibling ``.tmp`` path, so it needs ``O_NOFOLLOW`` too.
    staging_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(staging_fd, "w") as staging_file:
        json.dump(payload, staging_file)
    temporary.replace(_DEPLOYMENT_LEASE_PATH)


def _release_requested() -> bool:
    return os.environ.get(_RELEASE_DEPLOYMENT_ENV, "").lower() in {"1", "true", "yes"}


def _stop_modal_app(app_identifier: str, *, workspace: str) -> bool:
    """Stop a deployed app without prompting, returning whether it succeeded."""
    try:
        result = subprocess.run(  # noqa: S603  # tracked: #288
            ["uv", "run", "modal", "app", "stop", app_identifier, "--yes"],  # noqa: S607  # tracked: #288
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(  # noqa: T201  # tracked: #288
            f"Could not stop Modal app {app_identifier}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return False
    if result.returncode == 0:
        print(f"Stopped Modal app {app_identifier}.", file=sys.stderr)  # noqa: T201  # tracked: #288
        return True
    output = f"{result.stdout}\n{result.stderr}".strip()
    print(  # noqa: T201  # tracked: #288
        f"Could not stop Modal app {app_identifier} (exit {result.returncode}): {output[-2000:]}",
        file=sys.stderr,
    )
    return False


def _retire_deployment(lease: _DeploymentLease, *, workspace: str) -> None:
    """Best-effort stop and forget a lease that must not be reused."""
    if lease.app_identifier is not None:
        _stop_modal_app(lease.app_identifier, workspace=workspace)
    _DEPLOYMENT_LEASE_PATH.unlink(missing_ok=True)


def _deployment_path(workspace: str, entrypoint: str) -> Path:
    """Resolve a candidate-owned Modal file without allowing project escapes."""
    relative = Path(entrypoint)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("Modal entrypoint must be a project-relative path")  # noqa: TRY003
    workspace_root = Path(workspace).resolve(strict=True)
    candidate = (workspace_root / relative).resolve(strict=True)
    try:
        candidate.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError(f"Modal entrypoint escapes the project: {entrypoint}") from exc  # noqa: TRY003
    if not candidate.is_file():
        raise ValueError(f"Modal entrypoint is not a file: {candidate}")  # noqa: TRY003
    return candidate


@dataclass(frozen=True)
class _CommandTransferPlan:
    """Files to stage into the serving container and outputs to relay back."""

    stage_paths: tuple[str, ...]
    output_paths: tuple[str, ...]


def _uses_trusted_go_package_cwd(command: Sequence[str]) -> bool:
    if not command or Path(command[0]).name != "go" or command[1:2] != ["-C"]:
        return False
    try:
        go_cwd = Path(command[2])
    except IndexError:
        return False
    if go_cwd.is_absolute() or any(part in {"", ".", ".."} for part in go_cwd.parts):
        return False
    package_path = Path(_EVALUATOR_PACKAGE_STAGE_PATH)
    return go_cwd == package_path or go_cwd.is_relative_to(package_path)


def _plan_command_transfer(command: Sequence[str], workspace: str) -> _CommandTransferPlan:
    """Classify command tokens into staged inputs and relayed outputs.

    Workspace-relative tokens that resolve to files are staged with their
    parent directory (scripts commonly import or read siblings); directory
    tokens are staged whole. Absolute tokens that do not exist but whose
    parent directory does are treated as output artifacts the command will
    write, and are relayed back after the in-container run.
    """
    workspace_root = Path(workspace).resolve(strict=True)
    framework_go_cwd = _uses_trusted_go_package_cwd(command)
    staged: list[str] = []
    outputs: list[str] = []
    for token in command:
        if not token or token.startswith("-"):
            continue
        if token.startswith("/"):
            path = Path(token)
            if not path.exists() and path.parent.is_dir() and token not in outputs:
                outputs.append(token)
            continue
        if framework_go_cwd and token == ".":  # noqa: S105
            # ``go -C <trusted-package> run .`` resolves the dot below the
            # separately staged evaluator package, not the candidate root.
            continue
        candidate = workspace_root / token
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(workspace_root)
        except (OSError, ValueError):
            continue
        if resolved.is_dir() or resolved.parent == workspace_root:
            stage = resolved
        else:
            stage = resolved.parent
        relative = stage.relative_to(workspace_root).as_posix()
        if relative not in staged:
            staged.append(relative)

    def _covered(path: str) -> bool:
        return any(other != path and path.startswith(f"{other}/") for other in staged)

    kept = tuple(path for path in staged if not _covered(path))
    return _CommandTransferPlan(stage_paths=kept, output_paths=tuple(outputs))


def _build_stage_archive(
    workspace: str,
    stage_paths: Sequence[str],
    *,
    evaluator_package_root: str | None = None,
) -> bytes:
    """Produce the trusted evaluator's gzipped serving-container inputs."""
    workspace_root = Path(workspace).resolve(strict=True)
    package_root: Path | None = None
    if evaluator_package_root is not None:
        package_root = Path(evaluator_package_root).resolve(strict=True)
        if not package_root.is_dir():
            raise ValueError(  # noqa: TRY003
                f"evaluator package root is not a directory: {package_root}"
            )
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for relative in stage_paths:
            relative_path = Path(relative)
            reserved_paths = (
                Path(_EVALUATOR_PACKAGE_STAGE_PATH),
                Path(_EVALUATOR_TOOLS_STAGE_PATH),
                Path(_EVALUATOR_TOOLCHAINS_STAGE_PATH),
                Path(_FRAMEWORK_BIN_STAGE_PATH),
                Path(_FRAMEWORK_PIP_STAGE_PATH),
                Path(_FRAMEWORK_UV_CACHE_STAGE_PATH),
            )
            if relative_path == Path():
                for child in workspace_root.iterdir():
                    if child.name not in {
                        _EVALUATOR_PACKAGE_STAGE_PATH,
                        _EVALUATOR_TOOLS_STAGE_PATH,
                        _EVALUATOR_TOOLCHAINS_STAGE_PATH,
                        _FRAMEWORK_BIN_STAGE_PATH,
                        _FRAMEWORK_PIP_STAGE_PATH,
                        _FRAMEWORK_UV_CACHE_STAGE_PATH,
                    }:
                        archive.add(str(child), arcname=child.name)
                continue
            if any(
                relative_path == reserved
                or relative_path.is_relative_to(reserved)
                or reserved.is_relative_to(relative_path)
                for reserved in reserved_paths
            ):
                raise ValueError(  # noqa: TRY003
                    "workspace evaluator input collides with a reserved framework path"
                )
            archive.add(str(workspace_root / relative), arcname=relative)
        if package_root is not None:
            archive.add(str(package_root), arcname=_EVALUATOR_PACKAGE_STAGE_PATH)
    payload = buffer.getvalue()
    if len(payload) > _MAX_STAGE_ARCHIVE_BYTES:
        raise ValueError(  # noqa: TRY003
            f"evaluator inputs too large to stage into the serving container "
            f"({len(payload)} bytes compressed, cap {_MAX_STAGE_ARCHIVE_BYTES})"
        )
    return payload


def _find_app_container(
    app_identifier: str,
    *,
    workspace: str,
    base_url: str,
    timeout_seconds: float = _CONTAINER_DISCOVERY_TIMEOUT_SECONDS,
) -> str:
    """Return the id of a running container for the deployed app.

    Health probes double as warm-up: if the app scaled to zero between
    readiness and discovery, probing the public endpoint starts a container.
    """
    deadline = time.monotonic() + timeout_seconds
    last_error = "no container listed"
    while True:
        try:
            result = subprocess.run(  # tracked: #288
                ["uv", "run", "modal", "container", "list", "--json"],  # noqa: S607  # tracked: #288
                cwd=workspace,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            if result.returncode == 0:
                entries = json.loads(result.stdout)
            else:
                entries = []
                last_error = f"container list exited {result.returncode}: {result.stderr.strip()}"
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            entries = []
            last_error = f"{type(exc).__name__}: {exc}"
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            # Key style varies across modal client versions: 1.5.x emits
            # snake_case ("app_name"), older clients title case ("App Name").
            name = entry.get("app_name") or entry.get("App Name")
            container_id = entry.get("container_id") or entry.get("Container ID")
            if name == app_identifier and container_id:
                return str(container_id)
        if time.monotonic() >= deadline:
            raise TimeoutError(  # noqa: TRY003
                f"no running container found for Modal app {app_identifier}: {last_error}"
            )
        _healthy_now(base_url)
        time.sleep(5)


def _bootstrap_script(
    command: Sequence[str],
    output_paths: Sequence[str],
    *,
    setup_command: Sequence[str] | None = None,
) -> str:
    """Build the in-container POSIX shell bootstrap.

    Receives the staged archive as base64 chunks in ``"$@"``, recreates the
    workspace-relative layout in a temp directory, provides ``uv`` through an
    isolated Python shim, runs optional trusted setup and the evaluator argv
    verbatim from that directory, then emits each existing output file and the
    command's exit code between sentinel markers. ``modal container exec`` does
    not propagate exit codes itself.
    """
    quoted_command = " ".join(shlex.quote(token) for token in command)
    uv_wrapper = base64.b64encode(
        (
            "#!/bin/sh\n"
            'pip_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.pip" && pwd)\n'
            "exec python3 -I -c "
            "'import runpy,sys; sys.path.insert(0, sys.argv.pop(1)); "
            'runpy.run_module("uv", run_name="__main__")'
            '\' "$pip_root" "$@"\n'
        ).encode("ascii")
    ).decode("ascii")
    setup_block = ""
    if setup_command is not None:
        quoted_setup = " ".join(
            shlex.quote(token) for token in _normalized_setup_command(setup_command)
        )
        setup_block = f"""{quoted_setup}
setup_rc=$?
if [ "$setup_rc" -ne 0 ]; then
  printf 'vibesys trusted evaluator setup failed (exit %s)\\n' "$setup_rc" >&2
  printf '\\n{_EXEC_RC_MARKER}%s\\n' {_TRUSTED_SETUP_FAILURE_EXIT_CODE}
  exit 0
fi
"""
    relay_blocks = "\n".join(
        f"if [ -f {shlex.quote(path)} ]; then\n"
        f"  printf '\\n%s %s\\n' {shlex.quote(_OUTPUT_FILE_MARKER)} {shlex.quote(path)}\n"
        f"  base64 {shlex.quote(path)}\n"
        f"  printf '%s\\n' {shlex.quote(_OUTPUT_END_MARKER)}\n"
        "fi"
        for path in output_paths
    )
    return f"""set -u
stage=$(mktemp -d /tmp/vibesys-eval-XXXXXX)
printf '%s' "$@" | base64 -d | tar -xzf - -C "$stage"
rm -rf "$stage/.bin" "$stage/.pip" "$stage/.uv-cache"
mkdir -p "$stage/.bin"
printf '%s' {shlex.quote(uv_wrapper)} | base64 -d > "$stage/.bin/uv"
chmod +x "$stage/.bin/uv"
cd /tmp
if ! python3 -I -m pip install --quiet --target "$stage/.pip" uv >&2; then
  echo 'vibesys evaluator bootstrap failed: serving container lacks python3 -m pip' >&2
  printf '\\n{_EXEC_RC_MARKER}%s\\n' {_EVALUATOR_BOOTSTRAP_FAILURE_EXIT_CODE}
  exit 0
fi
PATH="$stage/.bin:$PATH"; export PATH
PYTHONPATH="$stage/.pip${{PYTHONPATH:+:$PYTHONPATH}}"; export PYTHONPATH
UV_CACHE_DIR="$stage/.uv-cache"; export UV_CACHE_DIR
cd "$stage"
{setup_block}if [ -d "$stage/.vibesys-evaluator-toolchains/cargo" ]; then
  RUSTUP_HOME="$stage/.vibesys-evaluator-toolchains/rustup"
  CARGO_HOME="$stage/.vibesys-evaluator-toolchains/cargo"
  export RUSTUP_HOME CARGO_HOME
fi
GOWORK=off; export GOWORK
{quoted_command}
rc=$?
{relay_blocks}
printf '\\n{_EXEC_RC_MARKER}%s\\n' "$rc"
"""


def _parse_exec_output(stdout: str) -> tuple[int | None, dict[str, bytes], str]:
    """Split exec stdout into exit code, relayed output files, and passthrough."""
    exit_code: int | None = None
    files: dict[str, bytes] = {}
    passthrough: list[str] = []
    collecting: str | None = None
    encoded: list[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if collecting is not None:
            if stripped == _OUTPUT_END_MARKER:
                try:
                    files[collecting] = base64.b64decode("".join(encoded))
                except ValueError:
                    files.pop(collecting, None)
                collecting = None
                encoded = []
            else:
                encoded.append(stripped)
            continue
        if stripped.startswith(f"{_OUTPUT_FILE_MARKER} "):
            collecting = stripped[len(_OUTPUT_FILE_MARKER) + 1 :]
            continue
        if stripped.startswith(_EXEC_RC_MARKER):
            suffix = stripped[len(_EXEC_RC_MARKER) :]
            if suffix.isdigit():
                exit_code = int(suffix)
            continue
        passthrough.append(line)
    return exit_code, files, "\n".join(passthrough)


class _DeploymentKeepWarm:
    """Probe the public endpoint periodically while the colocated run executes.

    Colocated traffic never crosses Modal ingress, so it does not reset the
    app's idle scaledown timer; without these probes Modal may retire the
    serving container mid-measurement.
    """

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> _DeploymentKeepWarm:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def _run(self) -> None:
        while not self._stop.wait(_KEEPWARM_INTERVAL_SECONDS):
            _healthy_now(self._base_url)


def _execute_colocated(  # noqa: PLR0913
    command: Sequence[str],
    *,
    workspace: str,
    app_identifier: str,
    base_url: str,
    setup_command: Sequence[str] | None = None,
    evaluator_package_root: str | None = None,
) -> int:
    """Run the trusted command inside the app's serving container."""
    plan = _plan_command_transfer(command, workspace)
    archive = _build_stage_archive(
        workspace,
        plan.stage_paths,
        evaluator_package_root=evaluator_package_root,
    )
    encoded = base64.b64encode(archive).decode("ascii")
    chunks = [
        encoded[offset : offset + _B64_CHUNK_CHARS]
        for offset in range(0, len(encoded), _B64_CHUNK_CHARS)
    ] or [""]
    container_id = _find_app_container(
        app_identifier,
        workspace=workspace,
        base_url=base_url,
    )
    script = _bootstrap_script(
        command,
        plan.output_paths,
        setup_command=setup_command,
    )
    with _DeploymentKeepWarm(base_url):
        result = subprocess.run(  # noqa: S603  # tracked: #288
            [  # noqa: S607  # tracked: #288
                "uv",
                "run",
                "modal",
                "container",
                "exec",
                container_id,
                "--",
                "sh",
                "-c",
                script,
                "vibesys-eval",
                *chunks,
            ],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
        )
    exit_code, files, passthrough = _parse_exec_output(result.stdout)
    if passthrough:
        print(passthrough)  # noqa: T201  # tracked: #288
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")  # noqa: T201  # tracked: #288
    for path, payload in files.items():
        Path(path).write_bytes(payload)
    if exit_code is None:
        tail = result.stdout[-_MAX_DIAGNOSTIC_CHARS:]
        print(  # noqa: T201  # tracked: #288
            "Modal evaluator exec did not report an exit code "
            f"(modal exit {result.returncode}); output tail:\n{tail}",
            file=sys.stderr,
        )
        return result.returncode or 1
    return exit_code


def run_evaluator(  # noqa: PLR0913
    command: Sequence[str],
    *,
    workspace: str = "/workspace",
    entrypoint: str = "main.py",
    readiness_timeout_seconds: float = 90,
    setup_command: Sequence[str] | None = None,
    evaluator_package_root: str | None = None,
) -> int:
    """Deploy the candidate and run ``command`` inside its serving container."""
    if not command:
        raise ValueError("missing evaluator command after '--'")  # noqa: TRY003  # tracked: #288
    normalized_setup = (
        _normalized_setup_command(setup_command) if setup_command is not None else None
    )
    normalized_package_root = (
        str(Path(evaluator_package_root).resolve(strict=True))
        if evaluator_package_root is not None
        else None
    )
    if normalized_package_root is not None and not Path(normalized_package_root).is_dir():
        raise ValueError(  # noqa: TRY003
            f"evaluator package root is not a directory: {normalized_package_root}"
        )

    with _exclusive_evaluation():
        return _run_evaluator_unlocked(
            command,
            workspace=workspace,
            entrypoint=entrypoint,
            readiness_timeout_seconds=readiness_timeout_seconds,
            setup_command=normalized_setup,
            evaluator_package_root=normalized_package_root,
        )


def _run_evaluator_unlocked(  # noqa: C901, PLR0911, PLR0912, PLR0913, PLR0915  # tracked: #288
    command: Sequence[str],
    *,
    workspace: str,
    entrypoint: str,
    readiness_timeout_seconds: float,
    setup_command: Sequence[str] | None,
    evaluator_package_root: str | None,
) -> int:
    candidate_revision = os.environ.get(_CANDIDATE_REVISION_ENV)
    if candidate_revision:
        lease = _read_deployment_lease()
        if lease is not None:
            if (
                lease.candidate_revision == candidate_revision
                and lease.app_identifier is not None
                and _healthy_now(lease.base_url)
            ):
                print(  # noqa: T201  # tracked: #288
                    "Reusing healthy Modal deployment for candidate revision "
                    f"{candidate_revision}.",
                    file=sys.stderr,
                )
                try:
                    return _execute_colocated(
                        command,
                        workspace=workspace,
                        app_identifier=lease.app_identifier,
                        base_url=lease.base_url,
                        setup_command=setup_command,
                        evaluator_package_root=evaluator_package_root,
                    )
                except (TimeoutError, ValueError) as exc:
                    print(f"Modal evaluator setup failed: {exc}", file=sys.stderr)  # noqa: T201
                    return 1
                finally:
                    if _release_requested():
                        _retire_deployment(lease, workspace=workspace)
            _retire_deployment(lease, workspace=workspace)

    try:
        deployment_path = _deployment_path(workspace, entrypoint)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Modal evaluator setup failed: {exc}", file=sys.stderr)  # noqa: T201
        return 1

    deploy = subprocess.run(  # noqa: S603  # tracked: #288
        ["uv", "run", "modal", "deploy", str(deployment_path)],  # noqa: S607  # tracked: #288
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    deploy_output = f"{deploy.stdout}\n{deploy.stderr}".strip()
    if deploy_output:
        print(deploy_output, file=sys.stderr)  # noqa: T201  # tracked: #288
    if deploy.returncode != 0:
        return deploy.returncode

    app_identifier: str | None = None
    try:
        base_url = extract_modal_web_url(deploy_output)
        app_identifier = extract_modal_app_identifier(deploy_output)
        wait_for_health(base_url, timeout_seconds=readiness_timeout_seconds)
    except (TimeoutError, ValueError) as exc:
        print(f"Modal evaluator setup failed: {exc}", file=sys.stderr)  # noqa: T201  # tracked: #288
        if app_identifier is None:
            try:  # noqa: SIM105  # tracked: #288
                app_identifier = extract_modal_app_identifier(deploy_output)
            except ValueError:
                pass
        if app_identifier is not None:
            print(  # noqa: T201  # tracked: #288
                f"Recent Modal logs:\n{recent_modal_logs(app_identifier, workspace=workspace)}",
                file=sys.stderr,
            )
            _stop_modal_app(app_identifier, workspace=workspace)
        return 1

    if candidate_revision:
        _write_deployment_lease(candidate_revision, base_url, app_identifier)

    try:
        return _execute_colocated(
            command,
            workspace=workspace,
            app_identifier=app_identifier,
            base_url=base_url,
            setup_command=setup_command,
            evaluator_package_root=evaluator_package_root,
        )
    except (TimeoutError, ValueError) as exc:
        print(f"Modal evaluator setup failed: {exc}", file=sys.stderr)  # noqa: T201  # tracked: #288
        return 1
    finally:
        if _release_requested():
            lease = _read_deployment_lease()
            if (
                candidate_revision
                and lease is not None
                and lease.candidate_revision == candidate_revision
            ):
                _retire_deployment(lease, workspace=workspace)
            else:
                _stop_modal_app(app_identifier, workspace=workspace)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default="/workspace")
    parser.add_argument("--entrypoint", default="main.py")
    parser.add_argument("--readiness-timeout-seconds", type=float, default=90)
    parser.add_argument("--setup-command-base64", type=_setup_command_argument)
    parser.add_argument("--evaluator-package-root")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:  # noqa: D103  # tracked: #288
    args = _parser().parse_args(argv)
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    return run_evaluator(
        command,
        workspace=args.workspace,
        entrypoint=args.entrypoint,
        readiness_timeout_seconds=args.readiness_timeout_seconds,
        setup_command=args.setup_command_base64,
        evaluator_package_root=args.evaluator_package_root,
    )


if __name__ == "__main__":
    raise SystemExit(main())
