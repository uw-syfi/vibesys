"""Relaunch admission and confirmation over the existing detached run launcher."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.api import ConfigurationError
from vibesys.api.request import ensure_resumable
from vs_project.api import Project
from vs_proto.api import Oneof, RelaunchRefusal, RelaunchResponse
from vs_runlog.api import (
    RunLaunchLockedError,
    RunLogStopReason,
    assert_run_unlocked,
    exclusive_run_launch,
    open_run_log,
)
from vs_sim.api import Capture, ProcessOutcome, ProcessSpec

if TYPE_CHECKING:
    from vs_proto.api import RelaunchRequest
    from vs_sim.api import Processes, Threads

__all__ = ["relaunch_host"]


def relaunch_host(
    request: RelaunchRequest, checkout: str, *, processes: Processes, threads: Threads
) -> RelaunchResponse:
    """Refuse before spawning; confirm success only from the writer's epoch record.

    The detached launcher owns execution after this bounded call returns. A lost
    reply is uncertain, never retried automatically. expected_epoch fences a
    later retry after the first writer has exited.
    """
    executable = Path(checkout).expanduser() / ".venv" / "bin" / "vibesys"
    if not executable.is_file():
        return _refused(RelaunchRefusal.CHECKOUT_MISSING)
    project = Project.open(request.project_root)
    project.state.load_run(request.run_id)
    project.state.require_local_run(request.run_id)
    path = Project.run_log_path_for(project.root, request.run_id)
    if not path.exists():
        return _refused(RelaunchRefusal.EPOCH_CHANGED)
    try:
        with exclusive_run_launch(path):
            return _launch(request, executable, path, processes, threads)
    except RunLaunchLockedError:
        return _refused(RelaunchRefusal.LOCKED)


def _launch(
    request: RelaunchRequest, executable: Path, path: Path, processes: Processes, threads: Threads
) -> RelaunchResponse:
    try:
        assert_run_unlocked(path)
    except RuntimeError:
        return _refused(RelaunchRefusal.LOCKED)
    epoch, offset = _epoch(path)
    if epoch != request.expected_epoch:
        return _refused(RelaunchRefusal.EPOCH_CHANGED)
    try:
        ensure_resumable(Path(request.project_root), request.run_id)
    except ConfigurationError as error:
        if error.diagnostic.code != "run_ended":
            raise
        return _refused(RelaunchRefusal.ENDED)
    deadline = threads.now() + 25
    result = processes.run(
        ProcessSpec(
            (
                str(executable),
                "--resume",
                request.run_id,
                "--detach",
                request.project_root,
            ),
            cwd=Path(request.project_root),
            max_output_bytes=65536,
        ),
        timeout=20,
        output=Capture(65536),
    )
    if not isinstance(result, ProcessOutcome) or result.returncode != 0:
        try:
            assert_run_unlocked(path)
        except RuntimeError:
            return _refused(RelaunchRefusal.LOCKED)
        message = "detached relaunch failed; inspect the run before retrying"
        raise ValueError(message)
    return _confirm_epoch(path, request.expected_epoch, offset, deadline, threads)


def _confirm_epoch(
    path: Path, expected: int, offset: int, deadline: float, threads: Threads
) -> RelaunchResponse:
    while threads.now() < deadline:
        epoch, offset = _epoch(path, offset)
        if epoch > expected:
            return RelaunchResponse(outcome=Oneof("epoch", epoch))
        threads.sleep(0.05)
    message = "new epoch not confirmed; inspect the run before retrying"
    raise ValueError(message)


def _epoch(path: Path, offset: int = 0) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    with open_run_log(path, offset) as reader:
        epoch = max(
            (
                record.epoch
                for record in reader
                if record.body and record.body.field == "epoch_started"
            ),
            default=0,
        )
        if reader.stop.reason == RunLogStopReason.CORRUPTION:
            raise ValueError(reader.stop.detail)
        return epoch, reader.stop.offset


def _refused(reason: RelaunchRefusal) -> RelaunchResponse:
    return RelaunchResponse(outcome=Oneof("refusal", reason))
