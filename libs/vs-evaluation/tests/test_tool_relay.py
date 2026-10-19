"""The tool relay: tools served on the host, forwarded from an agent's container.

The service and the client run on the simulated network, so a host that answers garbage, never
answers, or serves two calls at once is a deterministic test. The agent-side MCP server is
driven through an in-memory MCP session, so what an agent sees (the tools offered, a call's text,
a failure) is observed at the protocol, not by reading the server's internals.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import BaseModel, ConfigDict

from vs_evaluation.api.tool_relay import (
    RELAY_REPLY,
    SOCKET_ENV,
    TOKEN_ENV,
    RelayRefusal,
    RelayRefusedError,
    RelayUnavailableError,
    ToolRelayClient,
    ToolRelayService,
    build_relay_server,
    relay_check_descriptor,
    relay_mcp_descriptor,
)
from vs_mcp.api import ToolSpec
from vs_sim.api.testing import HANG_GUARD_S, SimNetwork, SimThreads, examples

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sim.api import Connection

_SOCKET = Path("/run/relay.sock")
_TOKEN = "issued-to-one-agent"  # noqa: S105  # lint-waiver: LW-731907 [S105]; a fixed fixture value.
_TIMEOUT_S = 10.0


class _EchoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    times: int = 1


class _NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Tools:
    """Two tools over a counter, so a test can see which calls reached a handler."""

    def __init__(self) -> None:
        self.calls = 0
        self.running = 0
        self.most_running = 0
        self.threads: SimThreads | None = None

    def echo(self, args: _EchoArgs) -> str:
        self.calls += 1
        return args.text * args.times

    def fail(self, args: _NoArgs) -> str:
        del args
        self.calls += 1
        message = "the run is gone"
        raise ValueError(message)

    def slow(self, args: _NoArgs) -> str:
        del args
        assert self.threads is not None
        self.running += 1
        self.most_running = max(self.most_running, self.running)
        self.threads.sleep(1.0)
        self.running -= 1
        return "done"

    def specs(self) -> tuple[ToolSpec[Any], ...]:
        return (
            ToolSpec("echo", "Repeat text.", _EchoArgs, self.echo),
            ToolSpec("fail", "Always fails.", _NoArgs, self.fail),
            ToolSpec("slow", "Takes a second.", _NoArgs, self.slow),
        )


class _Host:
    """A running relay service on a simulated network, and clients that dial it."""

    def __init__(self, threads: SimThreads, tools: _Tools) -> None:
        tools.threads = threads
        self.threads = threads
        self.network = SimNetwork(threads)
        self.service = ToolRelayService(
            tools.specs(),
            _SOCKET,
            token=_TOKEN,
            network=self.network,
            threads=threads,
            clock=threads,
        )

    def client(self, token: str = _TOKEN) -> ToolRelayClient:
        return ToolRelayClient(
            _SOCKET, token, network=self.network, clock=self.threads, timeout_s=_TIMEOUT_S
        )


def _serve(body: Callable[[_Host], object], tools: _Tools | None = None, *, seed: int = 0) -> Any:  # noqa: ANN401  # lint-waiver: LW-731908 [ANN401]; the helper returns whatever the test's body observed.
    threads = SimThreads(schedule_seed=seed)
    host = _Host(threads, tools if tools is not None else _Tools())

    def main() -> object:
        host.service.start()
        try:
            return body(host)
        finally:
            host.service.close()

    return threads.run(main)


def _raw(host: _Host, line: bytes) -> bytes:
    """Send one request line as it is and read the reply line."""
    connection = host.network.connect(str(_SOCKET), HANG_GUARD_S)
    connection.send(line + b"\n")
    reply = b""
    while not reply.endswith(b"\n"):
        chunk = connection.recv(65536, HANG_GUARD_S)
        if not chunk:
            break
        reply += chunk
    connection.close()
    return reply


# --- what the host serves ----------------------------------------------------------------


def test_the_host_offers_each_tool_with_the_schema_its_handler_validates() -> None:
    offered = _serve(lambda host: host.client().list_tools())

    assert [tool.name for tool in offered] == ["echo", "fail", "slow"]
    echo = offered[0]
    assert echo.description == "Repeat text."
    assert echo.input_schema == _EchoArgs.model_json_schema()


@settings(max_examples=examples(pr=15, nightly=300), deadline=None)
@given(text=st.text(max_size=40), times=st.integers(min_value=0, max_value=5))
def test_a_call_returns_what_the_handler_returned(text: str, times: int) -> None:
    result = _serve(lambda host: host.client().call("echo", {"text": text, "times": times}))

    assert (result.text, result.is_error) == (text * times, False)


def test_a_handler_that_raises_is_a_failed_result_and_the_service_keeps_serving() -> None:
    def body(host: _Host) -> tuple[Any, Any]:
        failed = host.client().call("fail", {})
        return failed, host.client().call("echo", {"text": "still up"})

    failed, after = _serve(body)

    assert (failed.is_error, failed.text) == (True, "ValueError: the run is gone")
    assert (after.is_error, after.text) == (False, "still up")


@pytest.mark.parametrize(
    "arguments",
    [{}, {"text": 3}, {"text": "x", "times": "many"}, {"text": "x", "extra": 1}],
)
def test_arguments_the_schema_refuses_are_a_failed_result_that_never_reaches_the_handler(
    arguments: dict[str, Any],
) -> None:
    tools = _Tools()

    result = _serve(lambda host: host.client().call("echo", arguments), tools)

    assert result.is_error
    assert "invalid arguments for echo" in result.text
    assert tools.calls == 0


def test_a_tool_the_host_does_not_offer_is_refused() -> None:
    def body(host: _Host) -> None:
        with pytest.raises(RelayRefusedError, match="unknown tool 'delete_run'"):
            host.client().call("delete_run", {})

    _serve(body)


def test_handlers_never_run_two_at_a_time() -> None:
    tools = _Tools()

    def body(host: _Host) -> None:
        workers = [
            host.threads.spawn(lambda: host.client().call("slow", {}), name=f"caller-{n}")
            for n in range(3)
        ]
        for worker in workers:
            worker.join(HANG_GUARD_S)

    _serve(body, tools)

    assert tools.most_running == 1


def test_a_relay_needs_a_token_and_distinct_tool_names() -> None:
    threads = SimThreads(schedule_seed=0)
    specs = _Tools().specs()
    common = {"network": SimNetwork(threads), "threads": threads, "clock": threads}

    with pytest.raises(ValueError, match="token"):
        ToolRelayService(specs, _SOCKET, token="", **common)
    with pytest.raises(ValueError, match="distinct"):
        ToolRelayService((specs[0], specs[0]), _SOCKET, token=_TOKEN, **common)


# --- who may ask --------------------------------------------------------------------------


@settings(max_examples=examples(pr=15, nightly=200), deadline=None)
@given(guess=st.text(max_size=30).filter(lambda text: text != _TOKEN))
def test_a_request_without_the_issued_token_is_refused_and_runs_nothing(guess: str) -> None:
    tools = _Tools()

    def body(host: _Host) -> None:
        other = host.client(guess)
        with pytest.raises(RelayRefusedError, match="token"):
            other.list_tools()
        with pytest.raises(RelayRefusedError, match="token"):
            other.call("echo", {"text": "read"})

    _serve(body, tools)

    assert tools.calls == 0


def test_a_ping_needs_no_token_and_says_nothing_else() -> None:
    def body(host: _Host) -> bytes:
        host.client("").ping()
        return _raw(host, b'{"action": "ping"}')

    reply = _serve(body)

    assert json.loads(reply) == {"kind": "pong"}


_GARBAGE = st.one_of(
    st.binary(max_size=64).map(lambda raw: raw.replace(b"\n", b"")),
    st.sampled_from(
        [b"{}", b"[]", b"null", b'{"action": "stop"}', b'{"action": "call", "tool": 3}']
    ),
)


@settings(max_examples=examples(pr=20, nightly=400), deadline=None)
@given(line=_GARBAGE)
def test_a_malformed_request_is_refused_and_runs_nothing(line: bytes) -> None:
    tools = _Tools()

    reply = _serve(lambda host: _raw(host, line), tools)

    decoded = RELAY_REPLY.validate_json(reply)
    assert isinstance(decoded, RelayRefusal)
    assert tools.calls == 0


# --- a host that is gone, slow or broken --------------------------------------------------


def _ask_scripted_host(
    answer: Callable[[Connection, SimThreads], None] | None,
) -> tuple[BaseException | None, float]:
    """One list call against a scripted host (``None``: nothing listens); the failure and time."""
    threads = SimThreads(schedule_seed=0)

    def main() -> tuple[BaseException | None, float]:
        network = SimNetwork(threads)
        listener = network.listen(str(_SOCKET)) if answer is not None else None

        def serve() -> None:
            assert listener is not None
            assert answer is not None
            connection = listener.accept(HANG_GUARD_S)
            try:
                answer(connection, threads)
            except OSError:
                pass
            finally:
                connection.close()

        if listener is not None:
            threads.spawn(serve, name="host")
        client = ToolRelayClient(
            _SOCKET, _TOKEN, network=network, clock=threads, timeout_s=_TIMEOUT_S
        )
        started = threads.now()
        try:
            client.list_tools()
        except RelayUnavailableError as error:
            return error, threads.now() - started
        finally:
            if listener is not None:
                listener.close()
        return None, threads.now() - started

    return threads.run(main)


@settings(max_examples=examples(pr=20, nightly=400), deadline=None)
@given(line=_GARBAGE)
def test_a_reply_that_is_not_a_reply_is_an_unavailable_relay(line: bytes) -> None:
    def answer(connection: Connection, _threads: SimThreads) -> None:
        connection.recv(4096, HANG_GUARD_S)
        connection.send(line + b"\n")

    error, _ = _ask_scripted_host(answer)

    assert isinstance(error, RelayUnavailableError)


def test_no_host_is_an_unavailable_relay_that_says_unreachable() -> None:
    error, _ = _ask_scripted_host(None)

    assert isinstance(error, RelayUnavailableError)
    assert "unreachable" in str(error)


def test_a_host_that_hangs_up_without_a_reply_is_an_unavailable_relay() -> None:
    error, _ = _ask_scripted_host(lambda connection, _threads: connection.recv(4096, HANG_GUARD_S))

    assert isinstance(error, RelayUnavailableError)
    assert "closed without a reply" in str(error)


@settings(max_examples=examples(pr=10, nightly=200), deadline=None)
@given(interval_s=st.floats(min_value=0.01, max_value=_TIMEOUT_S * 0.99))
def test_a_trickling_host_cannot_outlast_the_call_deadline(interval_s: float) -> None:
    def answer(connection: Connection, threads: SimThreads) -> None:
        connection.recv(4096, HANG_GUARD_S)
        for _ in range(100_000):
            connection.send(b" ")
            threads.sleep(interval_s)

    error, elapsed = _ask_scripted_host(answer)

    assert isinstance(error, RelayUnavailableError)
    assert elapsed <= _TIMEOUT_S + interval_s


# --- what an agent sees over MCP ----------------------------------------------------------


def _mcp_session(body: Callable[[Any], Any]) -> Any:  # noqa: ANN401  # lint-waiver: LW-731909 [ANN401]; the helper returns whatever the test's body observed.
    """Run ``body(client_session)`` against the agent-side server forwarding to a live host."""

    def over_host(host: _Host) -> object:
        client = host.client()
        server = build_relay_server(client, client.list_tools(), "vibesys-run")

        async def run() -> object:
            async with create_connected_server_and_client_session(server) as session:
                return await body(session)

        return asyncio.run(run())

    return _serve(over_host)


def test_the_agent_is_offered_exactly_the_hosts_tools() -> None:
    async def body(session: Any) -> list[tuple[str, str | None, dict[str, Any]]]:  # noqa: ANN401  # lint-waiver: LW-731910 [ANN401]; the MCP client session type is not part of this library's surface.
        listed = await session.list_tools()
        return [(tool.name, tool.description, tool.inputSchema) for tool in listed.tools]

    offered = _mcp_session(body)

    assert offered == [
        ("echo", "Repeat text.", _EchoArgs.model_json_schema()),
        ("fail", "Always fails.", _NoArgs.model_json_schema()),
        ("slow", "Takes a second.", _NoArgs.model_json_schema()),
    ]


def test_an_agent_call_returns_the_hosts_text_and_a_host_failure_is_a_tool_error() -> None:
    async def body(session: Any) -> tuple[str, bool, str, bool]:  # noqa: ANN401  # lint-waiver: LW-731911 [ANN401]; the MCP client session type is not part of this library's surface.
        await session.list_tools()  # the session learns the schemas, as an agent's does
        ok = await session.call_tool("echo", {"text": "ab", "times": 2})
        failed = await session.call_tool("fail", {})
        return (
            ok.content[0].text,
            ok.isError,
            failed.content[0].text,
            failed.isError,
        )

    ok_text, ok_error, failed_text, failed_error = _mcp_session(body)

    assert (ok_text, ok_error) == ("abab", False)
    assert failed_error is True
    assert "ValueError: the run is gone" in failed_text


# --- the descriptors ----------------------------------------------------------------------


def test_the_server_descriptor_carries_the_endpoint_and_token_as_launch_only_values() -> None:
    server = relay_mcp_descriptor("vibesys-run", "/run/relay.sock", "t0k3n")

    assert server.name == "vibesys-run"
    assert server.command == "python"
    assert server.args[:2] == ("-m", "vs_evaluation.tool_relay_mcp")
    assert dict(server.runtime_env) == {SOCKET_ENV: "/run/relay.sock", TOKEN_ENV: "t0k3n"}
    assert "t0k3n" not in repr(server)
    assert server.env == ()


def test_the_check_descriptor_has_the_servers_name_and_no_token() -> None:
    check = relay_check_descriptor("vibesys-run", "/run/relay.sock")

    assert check.name == "vibesys-run"
    assert check.args[-1] == "--check"
    assert dict(check.runtime_env) == {SOCKET_ENV: "/run/relay.sock"}
