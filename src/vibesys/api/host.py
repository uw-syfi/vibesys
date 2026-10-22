"""Typed host commands for control inbox writes and detached relaunch admission.

PostIntent acknowledges persistence; the run owns applying the request and
publishing its IntentState. Relaunch confirms the new writer epoch. No CLI or
gateway implementation enters this module.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from pydantic import ValidationError as ControlValidationError

from vibesys.api import (
    CONTROL_REQUEST,
    ConfigurationDiagnostic,
    ConfigurationError,
    ControlAction,
    recorded_ending,
)
from vs_project.api import INBOX_FORMAT, InboxClosedError, Project, ProjectError
from vs_proto.api import (
    ChatMessageRequest,
    ChatMessageResponse,
    ConfigurationFailed,
    HostCallRequest,
    HostCallResponse,
    Oneof,
    PostIntentResponse,
    SteerTargetKind,
    ValidationError,
    validate,
)
from vs_runlog.api import RunLock

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_proto.api import PostIntentRequest, SteerIntent
    from vs_sim.api import Processes, Threads

__all__ = ["host_call"]


def host_call(
    payload: bytes,
    *,
    processes: Processes,
    threads: Threads,
    start_chat: Callable[[ChatMessageRequest], None] | None = None,
) -> bytes:
    """Validate and execute one HostCall envelope, returning a typed response.

    Invalid input, unsupported intents and closed inboxes return the documented
    configuration_failed shape. Retrying a request id retains the inbox's exact
    ordering and payload-conflict semantics.
    """
    if len(payload) > 1 << 20:
        return _failure("request_too_large", "HostCall request exceeds 1 MiB")
    try:
        call = HostCallRequest.from_binary(payload)
        validate(call)
        match call.method:
            case Oneof("post_intent", request):
                result = _post_intent(request)
                response = HostCallResponse(result=Oneof("post_intent", result))
            case Oneof("relaunch", request):
                result = import_module("vibesys.api.host_relaunch").relaunch_host(
                    request, call.checkout, processes=processes, threads=threads
                )
                response = HostCallResponse(result=Oneof("relaunch", result))
            case Oneof("chat_message", request):
                result = _chat_message(request, start_chat)
                response = HostCallResponse(result=Oneof("chat_message", result))
            case _:
                return _failure("invalid_method", "HostCall.method is required")
    except ConfigurationError as error:
        diagnostic = error.diagnostic
        return _failure(diagnostic.code, diagnostic.message, exit_code=diagnostic.exit_code)
    except InboxClosedError as error:
        return _failure("inbox_closed", str(error), exit_code=3)
    except (ValueError, OSError, ProjectError, ControlValidationError, ValidationError) as error:
        return _failure("invalid_input", str(error))
    return response.to_binary()


def _failure(code: str, message: str, *, exit_code: int = 2) -> bytes:
    return HostCallResponse(
        result=Oneof(
            "configuration_failed",
            ConfigurationFailed(code=code, stage="host_call", message=message, exit_code=exit_code),
        )
    ).to_binary()


def _post_intent(request: PostIntentRequest) -> PostIntentResponse:
    control = CONTROL_REQUEST.validate_python(_control_payload(request))
    project = Project.open(request.project_root)
    project.state.load_run(request.run_id)
    project.state.require_local_run(request.run_id)
    inbox = project.state.control_inbox(request.run_id)
    inbox.post(
        control.control_id,
        control.model_dump_json().encode(),
        ended=lambda: recorded_ending(project, request.run_id),
    )
    return PostIntentResponse(
        request_id=control.control_id, ordered_reader=inbox.served_format() == INBOX_FORMAT
    )


def _control_payload(request: PostIntentRequest) -> dict[str, object]:
    common: dict[str, object] = {"control_id": request.request_id}
    match request.intent:
        case Oneof("pause_now", _):
            payload = {"action": "pause_now", "expected_epoch": request.expected_epoch}
        case Oneof("pause", _):
            payload = {"action": ControlAction.PAUSE, "expected_epoch": request.expected_epoch}
        case Oneof("steer", value):
            payload = _steer_payload(value)
        case Oneof("withdraw", value):
            payload = {"action": ControlAction.WITHDRAW, "withdraws": value.withdraws}
        case Oneof("cancel_attempt", value):
            payload = {"action": ControlAction.CANCEL_ATTEMPT, "attempt": value.attempt}
        case Oneof("cancel_item", value):
            payload = {"action": ControlAction.CANCEL_ITEM, "item": value.item}
        case Oneof("set_parallelism", value):
            payload = {"action": ControlAction.SET_MAX_PARALLEL, "max_parallel": value.max_parallel}
        case _:
            message = "PostIntent.intent is required"
            raise ValueError(message)
    return {**common, **payload}


def _steer_payload(value: SteerIntent) -> dict[str, object]:
    target = value.target
    if target is None:
        message = "steer.target is required"
        raise ValueError(message)
    return {
        "action": ControlAction.STEER,
        "text": value.text,
        "mode": value.mode.name.lower(),
        "target": {
            "kind": target.kind.name.lower(),
            "id": target.id if target.kind != SteerTargetKind.RUN else None,
        },
    }


def _chat_message(
    request: ChatMessageRequest, start: Callable[[ChatMessageRequest], None] | None
) -> ChatMessageResponse:
    if start is None:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="unavailable",
                stage="host_call",
                message="chat process launcher was not supplied",
            )
        )
    project = Project.open(request.project_root)
    project.state.load_run(request.run_id)
    project.state.require_local_run(request.run_id)
    import_module("vibesys.api.chat").run_head(
        Project.run_log_path_for(request.project_root, request.run_id).parent / "run.head"
    )
    directory = Project.chat_session_directory_for(
        request.project_root, request.run_id, request.session_id
    )
    directory.mkdir(parents=True, exist_ok=True)
    gate = RunLock(directory / "admission.lock")
    try:
        inbox = project.state.session_inbox(request.run_id, request.session_id)
        inbox.post(request.request_id, request.to_binary(), ended=lambda: None)
        start(request)
    finally:
        gate.close()
    return ChatMessageResponse(request_id=request.request_id, session_id=request.session_id)
