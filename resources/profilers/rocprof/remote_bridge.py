"""Dispatch ROCprof MCP captures through the configured Slurm connector."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import tempfile
import uuid
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Protocol, TypedDict, cast

from vs_sandbox.api.slurm import (
    load_slurm_policy,
    read_slurm_capture_plan,
    run_brokered_process,
)
from vs_slurm.api import (
    PORT_PLACEHOLDER,
    SlurmFileArtifact,
    SlurmJobRequest,
    SlurmJobRunner,
    SlurmTreeArtifact,
    load_slurm_config,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from threading import Event

    from vs_slurm.api import SlurmJobResult

_CAPTURE_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_PORT_FILE = ".vibesys-profile-port"
_MIN_GRACE_SECONDS = 30.0
_MAX_GRACE_SECONDS = 120.0
_GRACE_FRACTION = 0.1
_MIN_JOB_MARGIN_SECONDS = 10.0
_MAX_JOB_MARGIN_SECONDS = 120.0
_JOB_MARGIN_FRACTION = 0.1


class RemoteCaptureError(ValueError):
    """The remote job returned an invalid ROCprof result."""

    @classmethod
    def malformed_result(cls) -> RemoteCaptureError:
        """Describe a result that does not satisfy the bridge contract."""
        return cls("remote ROCprof capture returned a malformed result")


class _JobRunner(Protocol):
    def run(self, request: SlurmJobRequest) -> SlurmJobResult: ...


class _Lifecycle(Protocol):
    command: str
    cwd: str | None
    env: dict[str, str]
    ready_command: str | None
    ready_timeout_s: float
    ready_interval_s: float
    load_command: str | None
    setup_command: str | None
    stop_signal: str
    grace_s: float
    timeout_s: float


class _CaptureResult(TypedDict):
    output: str
    capture_ids: list[str]
    profiles_path: str | None


class RemoteCaptureBridge:
    """Run the existing capture lifecycle remotely and mirror its traces locally."""

    def __init__(
        self,
        config_path: Path,
        workspace: Path,
        *,
        profile_root: Path,
        evaluator_plan: Path | None = None,
        runner: _JobRunner | None = None,
    ) -> None:
        """Bind one candidate workspace and external operator config."""
        self._config_path = config_path.expanduser()
        self._workspace = workspace.resolve(strict=True)
        self._profile_root = profile_root.expanduser().resolve()
        self._policy = load_slurm_policy(self._config_path)
        self._plan = read_slurm_capture_plan(evaluator_plan) if evaluator_plan is not None else None
        self._support_trees = self._plan.support_paths if self._plan is not None else None
        self._config = load_slurm_config(self._config_path)
        broker_socket = os.environ.get("VIBESYS_SLURM_BROKER_SOCKET")
        broker_token = os.environ.get("VIBESYS_SLURM_BROKER_TOKEN")
        if runner is not None:
            self._runner = runner
        elif broker_socket is not None and broker_token is not None:
            self._runner = SlurmJobRunner(
                self._config,
                process=lambda argv, *, stdin, timeout: run_brokered_process(
                    Path(broker_socket),
                    broker_token,
                    argv,
                    stdin=stdin,
                    timeout=timeout,
                ),
            )
        else:
            self._runner = SlurmJobRunner(self._config)
        self._capture_lock = Lock()

    def configured_lifecycle(self) -> dict[str, object] | None:
        """Return the environment-owned serving capture lifecycle, when declared.

        The recipe is intentionally consumed inside the profiler MCP server.
        Agents invoke a semantic configured-capture tool and do not need to
        reconstruct operator commands, scheduler limits, or port management.
        """
        service = self._policy.remote_service()
        if service is None or self._plan is None or self._plan.benchmark_command is None:
            return None
        job_timeout_s = float(self._config.job_timeout_seconds)
        completion_margin_s = min(
            _MAX_JOB_MARGIN_SECONDS,
            max(_MIN_JOB_MARGIN_SECONDS, job_timeout_s * _JOB_MARGIN_FRACTION),
        )
        timeout_s = max(1.0, job_timeout_s - completion_margin_s)
        grace_s = min(
            _MAX_GRACE_SECONDS,
            max(_MIN_GRACE_SECONDS, timeout_s * _GRACE_FRACTION),
        )
        ready_timeout_s = min(
            float(service.startup_timeout_seconds),
            max(1.0, timeout_s - grace_s),
        )
        read_port = f"read -r PORT < {shlex.quote(_PORT_FILE)}"
        setup_command = (
            f"{shlex.quote(self._policy.remote_python)} -c "
            + shlex.quote(
                "import socket; "
                "s=socket.socket(); "
                "s.bind(('127.0.0.1', 0)); "
                "print(s.getsockname()[1]); "
                "s.close()"
            )
            + f" > {shlex.quote(_PORT_FILE)}"
        )
        readiness_probe = _shell_join_dynamic(
            (
                self._policy.remote_python,
                "-c",
                (
                    "import sys, urllib.request; "
                    "urllib.request.urlopen(sys.argv[1], timeout=2).close()"
                ),
                service.readiness_url,
            )
        )
        benchmark_command = (*self._plan.benchmark_command, *self._policy.benchmark_arguments)
        return {
            "command": f"{read_port}\n{_shell_join_dynamic(service.command)}",
            "cwd": None,
            "env": {},
            "ready_command": f"{read_port}\n{readiness_probe}",
            "ready_timeout_s": ready_timeout_s,
            "ready_interval_s": 1.0,
            "load_command": f"{read_port}\n{_shell_join_dynamic(benchmark_command)}",
            "setup_command": setup_command,
            "stop_signal": "SIGINT",
            "grace_s": grace_s,
            "timeout_s": timeout_s,
        }

    def capture(
        self,
        kind: str,
        lifecycle: _Lifecycle,
        options: Mapping[str, object],
        *,
        cancel_event: Event,
    ) -> str:
        """Submit one bounded profile operation and copy its capture into the MCP store."""
        if options.get("target") is not None:
            return (
                "error: persistent profiler targets are local to one MCP process; "
                "pass command and load_command to one profile_* call for remote Slurm"
            )
        if not self._capture_lock.acquire(blocking=False):
            return (
                "error: a remote Slurm ROCprof capture is already in progress; "
                "wait for it to finish before starting another"
            )
        try:
            return self._capture_owned(kind, lifecycle, options, cancel_event=cancel_event)
        finally:
            self._capture_lock.release()

    def _capture_owned(
        self,
        kind: str,
        lifecycle: _Lifecycle,
        options: Mapping[str, object],
        *,
        cancel_event: Event,
    ) -> str:
        """Run a capture after this bridge has acquired exclusive ownership."""
        self._profile_root.mkdir(parents=True, exist_ok=True)
        capture_root = ".vibesys-profile-output"
        result_path = f".vibesys-rocprof-result-{uuid.uuid4().hex}.json"
        request = {
            "kind": kind,
            "lifecycle": {
                "command": lifecycle.command,
                "cwd": lifecycle.cwd,
                "env": lifecycle.env,
                "ready_command": lifecycle.ready_command,
                "ready_timeout_s": lifecycle.ready_timeout_s,
                "ready_interval_s": lifecycle.ready_interval_s,
                "load_command": lifecycle.load_command,
                "setup_command": lifecycle.setup_command,
                "stop_signal": lifecycle.stop_signal,
                "grace_s": lifecycle.grace_s,
                "timeout_s": lifecycle.timeout_s,
            },
            "options": dict(options),
            "local_workspace": str(self._workspace),
        }
        command = (
            self._policy.remote_python,
            "rocprof_profiler/remote_capture.py",
            "--request-json",
            json.dumps(request, separators=(",", ":")),
            "--result",
            result_path,
            "--profiles",
            capture_root,
        )
        with tempfile.TemporaryDirectory(
            prefix=".vibesys-rocprof-remote-", dir=self._workspace
        ) as temporary:
            output_directory = Path(temporary) / "profiles"
            result_file = Path(temporary) / "result.json"
            result = self._runner.run(
                SlurmJobRequest(
                    workspace=self._workspace,
                    command=command,
                    setup_script=self._policy.setup_script,
                    support_trees=self._support_trees,
                    file_artifacts=(
                        SlurmFileArtifact(
                            remote_path=result_path,
                            local_path=result_file,
                        ),
                    ),
                    tree_artifacts=(
                        SlurmTreeArtifact(
                            remote_path=capture_root,
                            local_path=output_directory,
                        ),
                    ),
                    cancel_event=cancel_event,
                )
            )
            if result.exit_code != 0:
                detail = result.output.strip()
                return f"error: remote ROCprof capture job failed ({result.exit_code})\n{detail}"
            envelope = _load_result(result_file)
            capture_ids = envelope["capture_ids"]
            for capture_id in capture_ids:
                source = output_directory / capture_id
                if not source.is_dir():
                    raise RemoteCaptureError.malformed_result()
                destination = self._profile_root / capture_id
                if destination.exists():
                    raise RemoteCaptureError.malformed_result()
                shutil.move(source, destination)
            remote_root = envelope.get("profiles_path")
            output = envelope["output"]
            if isinstance(remote_root, str):
                output = output.replace(remote_root, str(self._profile_root))
            return output

    def capabilities(self) -> str:
        """Describe this configured remote capture route without submitting a job."""
        return remote_capabilities(self._config_path)


def _load_result(path: Path) -> _CaptureResult:
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RemoteCaptureError.malformed_result() from exc
    if not isinstance(envelope, dict) or not isinstance(envelope.get("output"), str):
        raise RemoteCaptureError.malformed_result()
    capture_ids = envelope.get("capture_ids")
    if not isinstance(capture_ids, list) or any(
        not isinstance(item, str) or _CAPTURE_ID.fullmatch(item) is None for item in capture_ids
    ):
        raise RemoteCaptureError.malformed_result()
    profiles_path = envelope.get("profiles_path")
    if profiles_path is not None and not isinstance(profiles_path, str):
        raise RemoteCaptureError.malformed_result()
    return cast("_CaptureResult", envelope)


def remote_capabilities(config_path: Path) -> str:
    """Describe configured remote capture without probing or allocating a GPU job."""
    config = load_slurm_config(config_path)
    return (
        f"Execution: remote Slurm connector {config.name!r}. Captures run the normal "
        "ROCprof lifecycle inside one job and copy trace files back for local analysis. "
        "Remote ROCm tool and GPU capabilities are validated by the first capture request. "
        "Persistent warm targets are unavailable across job-scoped captures."
    )


def _shell_join_dynamic(arguments: tuple[str, ...]) -> str:
    """Quote argv while substituting the environment-owned dynamic port."""
    return " ".join(_substitute_dynamic_port(argument) for argument in arguments)


def _substitute_dynamic_port(value: str) -> str:
    """Quote opaque text while retaining one shell port expansion."""
    if PORT_PLACEHOLDER not in value:
        return shlex.quote(value)
    before, after = value.split(PORT_PLACEHOLDER, maxsplit=1)
    return f'{shlex.quote(before)}"${{PORT}}"{shlex.quote(after)}'
