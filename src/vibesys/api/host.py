"""Typed host commands for control inbox writes and detached relaunch admission.

PostIntent acknowledges persistence; the run owns applying the request and
publishing its IntentState. Relaunch confirms the new writer epoch. No CLI or
gateway implementation enters this module.
"""

from __future__ import annotations

from functools import partial
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
    CreateChatSessionRequest,
    CreateChatSessionResponse,
    HostCallRequest,
    HostCallResponse,
    Oneof,
    PostIntentResponse,
    SteerTargetKind,
    ValidationError,
    host_operations,
    validate,
)
from vs_runlog.api import RunLock

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_proto.api import PostIntentRequest, RelaunchRequest, RelaunchResponse, SteerIntent
    from vs_sim.api import Platform, Threads, WriterProcessSignaller

__all__ = ["host_call", "terminate_host"]


def host_call(
    payload: bytes,
    *,
    platform: Platform,
    start_chat: Callable[[ChatMessageRequest], None] | None = None,
) -> bytes:
    """Validate and execute one HostCall envelope, returning a typed response.

    Invalid input, unsupported intents and closed inboxes return the documented
    configuration_failed shape. Retrying a request id retains the inbox's exact
    ordering and payload-conflict semantics. The gateway supplies its absolute
    deadline; direct callers without one use the registered operation cap.
    """
    threads = platform.threads
    if len(payload) > 1 << 20:
        return _failure("request_too_large", "HostCall request exceeds 1 MiB")
    try:
        call = HostCallRequest.from_binary(payload)
        validate(call)
        method = call.method
        result = None
        if method is not None:

            def relaunch(request: RelaunchRequest) -> RelaunchResponse:
                return import_module("vibesys.api.host_relaunch").relaunch_host(
                    request, call.checkout, processes=platform.processes, threads=threads
                )

            def chat_message(request: ChatMessageRequest) -> ChatMessageResponse:
                return _chat_message(request, start_chat)

            def create_chat_session(request: CreateChatSessionRequest) -> CreateChatSessionResponse:
                chat = import_module("vibesys.api.chat")
                return chat.create_session(request, chat.ChatRuntime(platform.clock, threads))

            result = host_operations.dispatch_host_call(
                method.field,
                method.value,
                post_intent=partial(
                    _apply_intent,
                    signaller=platform.writer_signaller,
                    threads=threads,
                    deadline=threads.now()
                    + (
                        call.deadline.to_seconds() - platform.clock.now()
                        if call.deadline is not None
                        else next(
                            op.cap_ms
                            for op in host_operations.HOST_CALLS
                            if op.slot == method.field
                        )
                        / 1000
                    ),
                ),
                relaunch=relaunch,
                chat_message=chat_message,
                create_chat_session=create_chat_session,
            )
        if result is None:
            return _failure("invalid_method", "HostCall.method is required")
        response = HostCallResponse(result=result)
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


def _apply_intent(
    request: PostIntentRequest,
    *,
    signaller: WriterProcessSignaller | None,
    threads: Threads,
    deadline: float,
) -> PostIntentResponse:
    if request.intent is not None and request.intent.field == "pause_now":
        return import_module("vibesys.api.host_terminate").pause_now(
            request, persist=_post_intent, signaller=signaller, threads=threads, deadline=deadline
        )
    return _post_intent(request)


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
        Project.run_head_path_for(request.project_root, request.run_id)
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


def terminate_host(payload: bytes, *, platform: Platform) -> bytes:
    """Serve internal escalation with the same durable Pause-now inbox owner."""
    return import_module("vibesys.api.host_terminate").serve_terminate(
        payload, persist=_post_intent, platform=platform
    )
