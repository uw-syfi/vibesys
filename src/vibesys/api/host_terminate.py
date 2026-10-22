"""Pause now owns durable cancellation, writer fencing and bounded process escalation.

A process-identity lifetime lock proves writer ownership. The signaller pins its
kernel handle, then proves that process owns the exact lock inode before signalling.
No response manufactures a lifecycle or writes a recovery interruption.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from vs_project.api import Project
from vs_proto.api import (
    MutateRequest,
    MutateResponse,
    Oneof,
    PauseNowIntent,
    PostIntentRequest,
    ValidationError,
    host_operations,
    validate,
)
from vs_proto.api import host_payloads as pb
from vs_runlog.api import (
    RunLaunchLockedError,
    WriterIdentityState,
    assert_run_unlocked,
    exclusive_run_launch,
    observe_writer_identity,
    read_current_epoch,
    read_run_head,
    writer_identity_path,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_proto.api import PostIntentResponse, RunProcess
    from vs_sim.api import Threads, WriterProcessSignaller

__all__ = ["pause_now", "serve_terminate", "terminate_run"]

_POLICY = host_operations.MUTATIONS["terminate_run"]


class _Observation(StrEnum):
    HELD = "held"
    RELEASED = "released"
    CHANGED = "changed"
    UNKNOWN = "unknown"


def _observe(project: Project, run_id: str, expected: RunProcess) -> _Observation:
    path = Project.run_log_path_for(project.root, run_id)
    try:
        head = read_run_head(path)
        if head.epoch != expected.epoch or head.process != expected:
            return _Observation.CHANGED
        identity = observe_writer_identity(path, expected)
        if identity is WriterIdentityState.HELD:
            return _Observation.HELD
        if identity is WriterIdentityState.UNKNOWN:
            return _Observation.UNKNOWN
        try:
            assert_run_unlocked(path)
        except RuntimeError:
            return _Observation.CHANGED
        else:
            return _Observation.RELEASED
    except (OSError, ValueError, ValidationError):
        return _Observation.UNKNOWN


def _await(
    project: Project, request: pb.TerminateRunRequest, deadline: float, threads: Threads
) -> _Observation:
    expected = request.expected_process
    if expected is None:
        return _Observation.UNKNOWN
    while (observed := _observe(project, request.run_id, expected)) is _Observation.HELD:
        remaining = deadline - threads.now()
        if remaining <= 0:
            return observed
        threads.sleep(min(0.05, remaining))
    return observed


def _response(request_id: str, observed: _Observation) -> pb.TerminateRunResponse:
    outcome = {
        _Observation.HELD: pb.TerminateRunOutcome.REQUESTED_UNCONFIRMED,
        _Observation.RELEASED: pb.TerminateRunOutcome.WRITER_RELEASED,
        _Observation.CHANGED: pb.TerminateRunOutcome.IDENTITY_CHANGED,
        _Observation.UNKNOWN: pb.TerminateRunOutcome.OBSERVATION_UNKNOWN,
    }[observed]
    return pb.TerminateRunResponse(request_id=request_id, outcome=outcome)


def _fenced_observation(
    project: Project, request: pb.TerminateRunRequest, deadline: float, threads: Threads
) -> _Observation:
    expected = request.expected_process
    observed = (
        _Observation.UNKNOWN if expected is None else _observe(project, request.run_id, expected)
    )
    return _Observation.UNKNOWN if threads.now() >= deadline else observed


def _signal_observation(
    project: Project,
    request: pb.TerminateRunRequest,
    deadline: float,
    threads: Threads,
    signaller: WriterProcessSignaller,
) -> _Observation:
    observed = _fenced_observation(project, request, deadline, threads)
    expected = request.expected_process
    if observed is _Observation.HELD and expected is not None:
        path = writer_identity_path(
            Project.run_log_path_for(project.root, request.run_id), expected
        )
        if not signaller.owns_lock(expected.pid, path):
            return _Observation.CHANGED
    return observed


def _escalate(
    project: Project,
    request: pb.TerminateRunRequest,
    *,
    signaller: WriterProcessSignaller | None,
    threads: Threads,
    deadline: float,
) -> pb.TerminateRunResponse:
    grace = threads.now() + _POLICY.grace_ms / 1000
    observed = _await(project, request, min(grace, deadline), threads)
    if observed is not _Observation.HELD:
        return _response(request.request_id, observed)
    if threads.now() >= deadline:
        return _response(request.request_id, _Observation.UNKNOWN)
    if signaller is None:
        return pb.TerminateRunResponse(
            request_id=request.request_id, outcome=pb.TerminateRunOutcome.UNSUPPORTED
        )
    expected = request.expected_process
    if expected is None:
        return _response(request.request_id, _Observation.UNKNOWN)

    def current() -> bool:
        nonlocal observed
        observed = _signal_observation(project, request, deadline, threads, signaller)
        return observed is _Observation.HELD

    try:
        sent = signaller.terminate_if_current(expected.pid, current)
    except ProcessLookupError:
        outcome = pb.TerminateRunOutcome.PROCESS_ABSENT
    except NotImplementedError:
        outcome = pb.TerminateRunOutcome.UNSUPPORTED
    except OSError:
        outcome = pb.TerminateRunOutcome.OBSERVATION_UNKNOWN
    else:
        return _response(
            request.request_id,
            _await(project, request, deadline, threads) if sent else observed,
        )
    return pb.TerminateRunResponse(request_id=request.request_id, outcome=outcome)


def terminate_run(
    request: pb.TerminateRunRequest,
    *,
    persist: Callable[[PostIntentRequest], PostIntentResponse],
    signaller: WriterProcessSignaller | None,
    threads: Threads,
) -> tuple[PostIntentResponse, pb.TerminateRunResponse]:
    """Persist Pause now first; serialize admission and signal only the fenced writer.

    Retrying the id reuses the inbox admission. It never relaunches a writer and
    never signals through a bare PID. Unknown identity cannot authorize a signal.
    """
    request = _POLICY.request_value(_POLICY.accept(request))
    project = Project.open(request.project_root)
    project.state.load_run(request.run_id)
    project.state.require_local_run(request.run_id)
    expected = request.expected_process
    if expected is None:
        message = "expected_process is required"
        raise ValueError(message)
    deadline = threads.now() + _POLICY.cap_ms / 1000
    acknowledgment = persist(
        PostIntentRequest(
            project_root=str(project.root),
            run_id=request.run_id,
            request_id=request.request_id,
            expected_epoch=expected.epoch,
            intent=Oneof("pause_now", PauseNowIntent()),
        )
    )
    path = Project.run_log_path_for(project.root, request.run_id)
    try:
        with exclusive_run_launch(path):
            epoch = read_current_epoch(path)
            response = (
                _response(request.request_id, _Observation.CHANGED)
                if epoch is None or epoch.number != expected.epoch
                else _escalate(
                    project, request, signaller=signaller, threads=threads, deadline=deadline
                )
            )
    except RunLaunchLockedError:
        response = _response(request.request_id, _Observation.UNKNOWN)
    return acknowledgment, response


def pause_now(
    request: PostIntentRequest,
    *,
    persist: Callable[[PostIntentRequest], PostIntentResponse],
    signaller: WriterProcessSignaller | None,
    threads: Threads,
) -> PostIntentResponse:
    """Keep process escalation inside the existing Pause-now admission owner."""
    try:
        head = read_run_head(Project.run_log_path_for(request.project_root, request.run_id))
    except (OSError, ValueError, ValidationError):
        head = None
    expected = None if head is None else head.process
    if expected is None or expected.epoch != request.expected_epoch:
        acknowledgment = persist(request)
        acknowledgment.termination = _response(
            request.request_id, _Observation.UNKNOWN if expected is None else _Observation.CHANGED
        )
        return acknowledgment
    acknowledgment, response = terminate_run(
        pb.TerminateRunRequest(
            request_id=request.request_id,
            project_root=request.project_root,
            run_id=request.run_id,
            expected_process=expected,
        ),
        persist=persist,
        signaller=signaller,
        threads=threads,
    )
    acknowledgment.termination = response
    return acknowledgment


def serve_terminate(
    payload: bytes,
    *,
    persist: Callable[[PostIntentRequest], PostIntentResponse],
    signaller: WriterProcessSignaller | None,
    threads: Threads,
) -> bytes:
    """Serve the generated internal TerminateRun mutation envelope."""
    envelope = MutateRequest.from_binary(payload)
    validate(envelope)
    method = envelope.method
    if method is None or method.field != "terminate_run":
        message = "Mutate.method must be terminate_run"
        raise ValueError(message)
    _, response = terminate_run(method.value, persist=persist, signaller=signaller, threads=threads)
    result = MutateResponse(result=Oneof("terminate_run", response))
    validate(result)
    return result.to_binary()
