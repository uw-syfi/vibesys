"""Dispatch ROCprof MCP captures through the configured Slurm connector."""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Protocol, TypedDict, cast

from vs_sandbox.api.slurm import (
    configured_capture_lifecycle,
    load_slurm_policy,
    read_slurm_capture_plan,
    run_brokered_process,
)
from vs_slurm.api import (
    SlurmFileArtifact,
    SlurmJobRequest,
    SlurmJobRunner,
    SlurmTreeArtifact,
    load_slurm_config,
)

_HERE = Path(__file__).resolve().parent
for _common_name in ("_common", "profilers_common"):
    _candidate = _HERE.parent / _common_name
    if (_candidate / "capture_runtime.py").is_file():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break
# Imported after the path setup above, which places the profiler common package.
capture_runtime = importlib.import_module("capture_runtime")

if TYPE_CHECKING:
    from collections.abc import Mapping
    from threading import Event

    from vs_slurm.api import SlurmJobResult

_CAPTURE_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")


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
            # The broker transfers only files under the run's own roots, and the
            # candidate workspace is one; the system temporary directory is not.
            self._runner = SlurmJobRunner(
                self._config,
                process=lambda argv, *, stdin, timeout: run_brokered_process(
                    Path(broker_socket),
                    broker_token,
                    argv,
                    stdin=stdin,
                    timeout=timeout,
                ),
                scratch_root=self._workspace,
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
        if self._plan is None:
            return None
        return configured_capture_lifecycle(
            self._config, self._policy, self._plan.benchmark_command
        )

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
            diagnostic = (
                "error: persistent profiler targets are local to one MCP process; "
                "pass command and load_command to one profile_* call for remote Slurm"
            )
            raise capture_runtime.CaptureFailedError.analysis_failed(diagnostic)
        if not self._capture_lock.acquire(blocking=False):
            diagnostic = (
                "error: a remote Slurm ROCprof capture is already in progress; "
                "wait for it to finish before starting another"
            )
            raise capture_runtime.CaptureFailedError.analysis_failed(diagnostic)
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
                diagnostic = (
                    f"error: remote ROCprof capture job failed ({result.exit_code})\n{detail}"
                )
                raise capture_runtime.CaptureFailedError.analysis_failed(diagnostic)
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
            # The remote job exits 0 for any capture it ran; a capture whose
            # workload did not run is a failure, not a profile to analyze.
            if not capture_ids:
                raise capture_runtime.CaptureFailedError("no_capture", output)
            failure = capture_runtime.workload_failure(self._profile_root, capture_ids)
            if failure is not None:
                raise capture_runtime.CaptureFailedError("workload_failed", f"{output}\n{failure}")
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
