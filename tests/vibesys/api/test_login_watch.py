"""A login watch writes one framed event and only then waits for the caller."""

from __future__ import annotations

import pytest

from vibesys.api.host_operations.api import LoginRefusedFact, LoginStartedFact, serve_login
from vs_proto.api import (
    FailureCategory,
    Oneof,
    WatchRequest,
    WatchResponse,
)
from vs_proto.api import (
    host_payloads as pb,
)


def _request(provider: str = "claude") -> bytes:
    return WatchRequest(
        checkout="/checkout",
        method=Oneof("login", pb.LoginRequest(request_id="request-a", provider=provider)),
    ).to_binary()


def _response(written: bytearray) -> WatchResponse:
    length = int.from_bytes(written[:4], "big")
    assert length == len(written) - 4
    return WatchResponse.from_binary(bytes(written[4:]))


def test_a_started_login_is_framed_before_the_watch_waits() -> None:
    order: list[str] = []

    def start(provider: str) -> LoginStartedFact:
        order.append(f"start:{provider}")
        return LoginStartedFact("Open https://a.example/x")

    def wait() -> None:
        order.append("wait")

    written = bytearray()
    serve_login(_request(), cli="login", start=start, write=written.extend, wait=wait)

    assert order == ["start:claude", "wait"]
    response = _response(written)
    assert response.result is not None
    assert response.result.field == "login"
    event = response.result.value
    assert event.request_id == "request-a"
    assert event.sequence == 1
    assert event.body is not None
    assert event.body.field == "started"
    assert event.body.value.instruction == "Open https://a.example/x"


def test_a_refusal_names_the_host_reason_and_still_waits_for_the_caller() -> None:
    def start(_provider: str) -> LoginRefusedFact:
        return LoginRefusedFact(
            reason="needs_terminal", command="codex login", detail="use a terminal"
        )

    written = bytearray()
    waited: list[bool] = []
    serve_login(
        _request(),
        cli="login",
        start=start,
        write=written.extend,
        wait=lambda: waited.append(True),
    )

    assert waited == [True]
    response = _response(written)
    assert response.result is not None
    assert response.result.field == "login"
    body = response.result.value.body
    assert body is not None
    assert body.field == "unavailable"
    assert body.value.reason == pb.LoginUnavailableReason.NEEDS_TERMINAL
    assert body.value.command == "codex login"
    assert body.value.detail == "use a terminal"


def test_an_invalid_watch_is_a_failure_frame_and_does_not_wait() -> None:
    written = bytearray()
    waited: list[bool] = []
    serve_login(
        b"\x00" * ((1 << 20) + 1),
        cli="login",
        start=lambda _provider: LoginStartedFact(None),
        write=written.extend,
        wait=lambda: waited.append(True),
    )

    assert waited == []
    response = _response(written)
    assert response.result is not None
    assert response.result.field == "failure"
    assert response.result.value.category == FailureCategory.INVALID_INPUT


@pytest.mark.parametrize("payload", [b"\xff", b"", WatchRequest(checkout="/checkout").to_binary()])
def test_malformed_or_missing_login_never_starts_or_waits(payload: bytes) -> None:
    calls: list[str] = []
    written = bytearray()
    serve_login(
        payload,
        cli="login",
        start=lambda provider: calls.append(provider) or LoginStartedFact(None),
        write=written.extend,
        wait=lambda: calls.append("wait"),
    )
    response = _response(written)
    assert response.result is not None
    assert response.result.field == "failure"
    assert response.result.value.category == FailureCategory.INVALID_INPUT
    assert calls == []


def test_a_mismatched_watch_command_never_starts_or_waits() -> None:
    calls: list[str] = []
    written = bytearray()
    serve_login(
        _request(),
        cli="other",
        start=lambda provider: calls.append(provider) or LoginStartedFact(None),
        write=written.extend,
        wait=lambda: calls.append("wait"),
    )
    response = _response(written)
    assert response.result is not None
    assert response.result.field == "failure"
    assert response.result.value.category == FailureCategory.INVALID_INPUT
    assert calls == []


@pytest.mark.parametrize(
    "reason", ["not_installed", "no_login_command", "needs_terminal", "start_failed", "unknown"]
)
def test_login_refusals_are_closed_and_unknown_reasons_do_not_wait(reason: str) -> None:
    written = bytearray()
    waited: list[bool] = []
    serve_login(
        _request(),
        cli="login",
        start=lambda _provider: LoginRefusedFact(reason),
        write=written.extend,
        wait=lambda: waited.append(True),
    )
    response = _response(written)
    assert response.result is not None
    assert response.result.field == ("failure" if reason == "unknown" else "login")
    assert waited == ([] if reason == "unknown" else [True])
