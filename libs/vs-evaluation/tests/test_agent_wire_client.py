"""The evaluation tool client gives the agent a typed error for every bad answer, within its deadline.

The client and a scripted service run on the simulated network and threads, so a
service that answers garbage or trickles bytes for ever is a deterministic,
virtual-time test.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from vs_evaluation.api.tools import EvaluationServiceClientError, SocketClient
from vs_sim.api.testing import HANG_GUARD_S, SimNetwork, SimThreads, examples

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sim.api import Connection

_ADDRESS = Path("/evaluation.sock")
_TIMEOUT_S = 10.0


class _Call(BaseModel):
    action: str = "submit"


def _read_request(connection: Connection) -> None:
    line = b""
    while not line.endswith(b"\n"):
        chunk = connection.recv(65536, HANG_GUARD_S)
        if not chunk:
            return
        line += chunk


def _call_against(
    answer: Callable[[Connection, SimThreads], None],
    *,
    buffer_bytes: int | None = None,
    reads: bool = True,
) -> tuple[BaseException | str, float]:
    """Run one client call against a service that answers with ``answer``; its outcome and virtual duration."""
    threads = SimThreads(schedule_seed=0)

    def main() -> tuple[BaseException | str, float]:
        network = SimNetwork(threads, buffer_bytes=buffer_bytes)
        listener = network.listen(str(_ADDRESS))

        def serve() -> None:
            connection = listener.accept(HANG_GUARD_S)
            try:
                if not reads:
                    # A service that stopped reading: it holds the connection, silent.
                    threads.sleep(_TIMEOUT_S * 10)
                    return
                _read_request(connection)
                with contextlib.suppress(OSError):
                    answer(connection, threads)
            finally:
                connection.close()

        threads.spawn(serve, name="service")
        client = SocketClient(_ADDRESS, network=network, clock=threads)
        started = threads.now()
        try:
            outcome: BaseException | str = client.call(_Call(), timeout_s=_TIMEOUT_S)
        except EvaluationServiceClientError as error:
            # Any other exception escapes and fails the test: that is the property.
            outcome = error
        finally:
            listener.close()
        return outcome, threads.now() - started

    return threads.run(main)


_GARBAGE = st.one_of(
    st.binary(max_size=64).map(lambda raw: raw.replace(b"\n", b"")),
    st.dictionaries(st.text(max_size=6), st.integers() | st.text(max_size=6), max_size=3).map(
        lambda result: json.dumps({"ok": True, "result": result}).encode()
    ),
    st.sampled_from([b"{}", b"[]", b"null", b'{"ok": false}', b'{"ok": "yes", "error": "x"}']),
)


@settings(max_examples=examples(pr=20, nightly=500), deadline=None)
@given(line=_GARBAGE)
def test_a_malformed_reply_is_the_typed_tool_error(line: bytes) -> None:
    def answer(connection: Connection, _threads: SimThreads) -> None:
        connection.send(line + b"\n")

    outcome, _elapsed = _call_against(answer)

    assert isinstance(outcome, EvaluationServiceClientError), repr(outcome)


@settings(max_examples=examples(pr=10, nightly=200), deadline=None)
@given(interval_s=st.floats(min_value=0.01, max_value=_TIMEOUT_S * 0.99))
def test_a_trickling_service_cannot_outlast_the_call_deadline(interval_s: float) -> None:
    def answer(connection: Connection, threads: SimThreads) -> None:
        for _ in range(100_000):
            connection.send(b"x")
            threads.sleep(interval_s)

    outcome, elapsed = _call_against(answer)

    assert isinstance(outcome, EvaluationServiceClientError), repr(outcome)
    assert elapsed <= _TIMEOUT_S + interval_s


def test_a_service_that_stops_reading_cannot_outlast_the_call_deadline() -> None:
    """The request does not fit the connection's buffer, and the service never reads it."""
    outcome, elapsed = _call_against(lambda _c, _t: None, buffer_bytes=4, reads=False)

    assert isinstance(outcome, EvaluationServiceClientError), repr(outcome)
    assert elapsed <= _TIMEOUT_S


def test_a_whole_reply_still_returns() -> None:
    """The guard against the two failures keeps the happy path: a refusal is passed through."""

    def answer(connection: Connection, _threads: SimThreads) -> None:
        connection.send(b'{"ok": false, "error": "no budget"}\n')

    outcome, _elapsed = _call_against(answer)

    assert isinstance(outcome, EvaluationServiceClientError)
    assert str(outcome) == "no budget"


@pytest.mark.parametrize("cut", [b"", b'{"ok": fa'])
def test_a_reply_cut_before_its_line_ends_is_the_typed_tool_error(cut: bytes) -> None:
    def answer(connection: Connection, _threads: SimThreads) -> None:
        connection.send(cut)

    outcome, _elapsed = _call_against(answer)

    assert isinstance(outcome, EvaluationServiceClientError), repr(outcome)
