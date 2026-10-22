"""The binary login entrypoint holds and cancels the provider-owned process."""

from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING

import pytest

from entrypoints.host import watch_login
from entrypoints.host_helper import AgentLibraryLogins
from server.host_helper_os import LoginBounds, LoginStarter
from vs_agent.api import LoginCommand, LoginState, ProviderLogin
from vs_proto.api import FailureCategory, Oneof, WatchRequest, WatchResponse, validate
from vs_proto.api import host_payloads as pb
from vs_sim.api import Signals
from vs_sim.api.testing import (
    HANG_GUARD_S,
    FakePipedProcessLauncher,
    FakeSignalRelay,
    ScriptedPipedProcess,
    SimThreads,
)

if TYPE_CHECKING:
    from collections.abc import Buffer


class _Output(BytesIO):
    def __init__(self, threads: SimThreads) -> None:
        super().__init__()
        self.ready = threads.event()

    def write(self, buffer: Buffer, /) -> int:
        length = super().write(buffer)
        self.ready.set()
        return length


def _request(provider: str = "codex") -> bytes:
    return WatchRequest(
        checkout="/checkout",
        method=Oneof("login", pb.LoginRequest(request_id="login-a", provider=provider)),
    ).to_binary()


def _library(
    threads: SimThreads, launcher: FakePipedProcessLauncher, *, installed: bool = True
) -> AgentLibraryLogins:
    return AgentLibraryLogins(
        lambda provider: ProviderLogin(
            provider=provider,
            installed=installed,
            state=LoginState.UNKNOWN,
            credential=None,
            api_key_variables=(),
            detail="",
        ),
        lambda _provider: LoginCommand(("codex", "login", "--device-auth"), needs_terminal=False),
        LoginStarter(launcher, threads, dict, LoginBounds()),
    )


def _response(output: BytesIO) -> WatchResponse:
    data = output.getvalue()
    assert int.from_bytes(data[:4], "big") == len(data) - 4
    response = WatchResponse.from_binary(data[4:])
    validate(response)
    return response


@pytest.mark.parametrize("ending", ["completion", "cancel"])
def test_the_watch_writes_its_event_before_holding_until_completion_or_cancel(ending: str) -> None:
    threads = SimThreads()
    process = ScriptedPipedProcess(threads, merge_stderr=True)
    process.write(stdout="Open https://login.example/device\nCode ABCD-EFGH1\n")
    launcher = FakePipedProcessLauncher(lambda _start: process)
    relay = FakeSignalRelay()
    output = _Output(threads)

    def run() -> None:
        watch = threads.spawn(
            lambda: watch_login(_request(), output, _library(threads, launcher), threads, relay),
            name="login-watch",
        )
        assert output.ready.wait(HANG_GUARD_S)
        response = _response(output)
        assert response.result is not None
        assert response.result.field == "login"
        body = response.result.value.body
        assert body is not None
        assert body.field == "started"
        assert (
            body.value.instruction
            == "Open https://login.example/device and enter the code ABCD-EFGH1"
        )
        assert relay.active
        assert watch.is_alive()
        if ending == "completion":
            process.exit(0)
        else:
            assert relay.deliver(Signals.SIGTERM)
        watch.join(HANG_GUARD_S)
        assert not watch.is_alive()
        assert not relay.active
        if ending == "cancel":
            assert process.signals == ["terminate"]

    threads.run(run)
    assert [start.argv for start in launcher.started] == [("codex", "login", "--device-auth")]


@pytest.mark.parametrize("provider", ["codex", "unoffered-provider"])
def test_a_refused_or_unknown_provider_is_framed_and_releases_the_watch(provider: str) -> None:
    threads = SimThreads()
    launcher = FakePipedProcessLauncher()
    relay = FakeSignalRelay()
    output = _Output(threads)
    threads.run(
        lambda: watch_login(
            _request(provider), output, _library(threads, launcher, installed=False), threads, relay
        )
    )
    response = _response(output)
    assert response.result is not None
    if provider == "codex":
        assert response.result.field == "login"
        body = response.result.value.body
        assert body is not None
        assert body.field == "unavailable"
        assert body.value.reason == pb.LoginUnavailableReason.NOT_INSTALLED
    else:
        assert response.result.field == "failure"
        assert response.result.value.category == FailureCategory.INVALID_INPUT
    assert not relay.active
    assert launcher.started == []
