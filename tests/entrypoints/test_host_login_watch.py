"""The binary login entrypoint holds and cancels the provider-owned process."""

from __future__ import annotations

from io import BufferedWriter, BytesIO, RawIOBase
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from entrypoints.host import watch_login
from server.host_agent_logins import AgentLibraryLogins, LoginBounds, LoginStarter
from vs_agent.api import LoginCommand, LoginState, ProviderLogin
from vs_proto.api import FailureCategory, Oneof, WatchRequest, WatchResponse, validate
from vs_proto.api import host_payloads as pb
from vs_sim.api import Connection, Signals
from vs_sim.api.testing import (
    HANG_GUARD_S,
    FakePipedProcessLauncher,
    FakeSignalRelay,
    ScriptedPipedProcess,
    SimNetwork,
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


class _PipeWriter(RawIOBase):
    """Real Python buffering over the simulator's byte pipe, with no OS handles."""

    def __init__(self, connection: Connection) -> None:
        super().__init__()
        self._connection = connection

    def writable(self) -> bool:
        return True

    def write(self, buffer: Buffer, /) -> int:
        data = bytes(buffer)
        self._connection.send(data, timeout=HANG_GUARD_S)
        return len(data)


def _read_pipe(connection: Connection, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = connection.recv(length - len(data), timeout=HANG_GUARD_S)
        assert chunk, "login output pipe closed inside its frame"
        data.extend(chunk)
    return bytes(data)


@pytest.mark.parametrize("ending", ["completion", "cancel"])
@given(buffer_size=st.integers(min_value=1, max_value=65536))
@example(buffer_size=8192)
def test_regression_login_instructions_leave_a_buffered_pipe_before_completion(
    buffer_size: int, ending: str
) -> None:
    threads = SimThreads()
    network = SimNetwork(threads)
    process = ScriptedPipedProcess(threads, merge_stderr=True)
    process.write(stdout="Open https://login.example/device\nCode ABCD-EFGH1\n")
    launcher = FakePipedProcessLauncher(lambda _start: process)
    relay = FakeSignalRelay()

    def run() -> None:
        listener = network.listen("login-output")
        sender = network.connect("login-output", timeout=HANG_GUARD_S)
        receiver = listener.accept(timeout=HANG_GUARD_S)
        output = BufferedWriter(_PipeWriter(sender), buffer_size=buffer_size)
        watch = threads.spawn(
            lambda: watch_login(_request(), output, _library(threads, launcher), threads, relay),
            name="buffered-login-watch",
        )
        try:
            size = int.from_bytes(_read_pipe(receiver, 4), "big")
            assert 0 < size < 1 << 20
            response = WatchResponse.from_binary(_read_pipe(receiver, size))
            validate(response)
            assert response.result is not None
            assert response.result.field == "login"
            body = response.result.value.body
            assert body is not None
            assert body.field == "started"
            assert body.value.instruction == (
                "Open https://login.example/device and enter the code ABCD-EFGH1"
            )
            assert watch.is_alive(), "instructions must be visible while login is still held"
            if ending == "completion":
                process.exit(0)
            else:
                assert relay.deliver(Signals.SIGTERM)
            watch.join(HANG_GUARD_S)
            assert not watch.is_alive()
            if ending == "cancel":
                assert process.signals == ["terminate"]
        finally:
            if watch.is_alive():
                process.exit(0)
            watch.join(HANG_GUARD_S)
            output.close()
            sender.close()
            receiver.close()
            listener.close()
        assert not watch.is_alive()
        assert not relay.active

    threads.run(run)
