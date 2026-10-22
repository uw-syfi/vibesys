"""One login watch: a framed first event, then the caller holds the process open.

The generated registry owns the budget and the request identity. This module
writes that one event and then waits. Closing a subscription is not a wait
ending; the caller ends the wait on cancel or when the login process exits.
"""

from collections.abc import Callable
from dataclasses import dataclass

from vs_proto.api import (
    FailureCategory,
    HostWatchRequest,
    HostWatchResponse,
    Oneof,
    OperationFailure,
    ValidationError,
    validate,
)
from vs_proto.api import (
    host_payloads as pb,
)

_MAX_BYTES = 1 << 20
_MAX_DIAGNOSTIC = 65536
_REASONS = {
    "not_installed": pb.LoginUnavailableReason.NOT_INSTALLED,
    "no_login_command": pb.LoginUnavailableReason.NO_LOGIN_COMMAND,
    "needs_terminal": pb.LoginUnavailableReason.NEEDS_TERMINAL,
    "start_failed": pb.LoginUnavailableReason.START_FAILED,
}


@dataclass(frozen=True, slots=True)
class LoginStartedFact:
    """The login command is running and has shown what it could."""

    instruction: str | None


@dataclass(frozen=True, slots=True)
class LoginRefusedFact:
    """The login was not started."""

    reason: str
    command: str | None = None
    detail: str = ""


def frame(payload: bytes) -> bytes:
    """One length-delimited payload, at most 1 MiB."""
    if len(payload) > _MAX_BYTES:
        message = "login frame exceeds 1 MiB"
        raise ValueError(message)
    return len(payload).to_bytes(4, "big") + payload


def serve_login(
    payload: bytes,
    *,
    cli: str,
    start: Callable[[str], LoginStartedFact | LoginRefusedFact],
    write: Callable[[bytes], object],
    wait: Callable[[], None],
) -> None:
    """Write the first login event, then ``wait`` until the caller ends the watch.

    ``wait`` runs only after a valid event. A refusal is still an event: the
    host has nothing left to hold, and ``wait`` returns immediately when the
    caller wires it that way.
    """
    if cli != "login":
        write(_failure(FailureCategory.INVALID_INPUT, "watch command does not match the envelope"))
        return
    if len(payload) > _MAX_BYTES:
        write(_failure(FailureCategory.INVALID_INPUT, "HostWatch request exceeds 1 MiB"))
        return
    try:
        request = HostWatchRequest.from_binary(payload)
        validate(request)
    except (ValidationError, ValueError) as exc:
        write(_failure(FailureCategory.INVALID_INPUT, str(exc)))
        return
    method = request.method
    if method is None or method.field != "login":
        write(_failure(FailureCategory.INVALID_INPUT, "HostWatch.method is required"))
        return
    login = method.value
    try:
        fact = start(login.provider)
        event = _event(login.request_id, fact)
        validate(event)
    except (ValidationError, ValueError, TypeError) as exc:
        write(_failure(FailureCategory.INVALID_INPUT, str(exc)))
        return
    write(frame(HostWatchResponse(result=Oneof("login", event)).to_binary()))
    wait()


def _event(request_id: str, fact: LoginStartedFact | LoginRefusedFact) -> pb.LoginEvent:
    if isinstance(fact, LoginStartedFact):
        body = pb.LoginStartedBody()
        if fact.instruction is not None:
            body = pb.LoginStartedBody(instruction=fact.instruction)
        return pb.LoginEvent(request_id=request_id, sequence=1, body=Oneof("started", body))
    reason = _REASONS.get(fact.reason)
    if reason is None:
        message = f"unknown login refusal {fact.reason!r}"
        raise ValueError(message)
    unavailable = pb.LoginUnavailableBody(reason=reason, detail=fact.detail)
    if fact.command is not None:
        unavailable = pb.LoginUnavailableBody(
            reason=reason, command=fact.command, detail=fact.detail
        )
    return pb.LoginEvent(request_id=request_id, sequence=1, body=Oneof("unavailable", unavailable))


def _failure(category: FailureCategory, message: str) -> bytes:
    text = message.strip() or "host login failed"
    encoded = text.encode()
    if len(encoded) > _MAX_DIAGNOSTIC:
        text = encoded[:_MAX_DIAGNOSTIC].decode(errors="ignore") or "host login failed"
    failure = OperationFailure(category=category, message=text)
    return frame(HostWatchResponse(result=Oneof("failure", failure)).to_binary())
