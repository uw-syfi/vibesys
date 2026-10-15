"""The queue status reader, its wire and its agent tool: read-only, typed, and bounded.

Every reader registered here passes the one contract suite. The tool server is driven
with synthesized tool calls, and the wire with a scripted service on the simulated
network, so a host that answers garbage or never answers is a deterministic test.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

from vs_evaluation.api.queue_status import (
    FRESH_FOR_S,
    QUEUE_READING,
    READ_QUEUE_STATUS_TOOL,
    CachedQueueStatusReader,
    LocalQueueStatusReader,
    OpaqueQueueStatusReader,
    QueueContention,
    QueueItem,
    QueueItemKind,
    QueueItemState,
    QueueKind,
    QueueStatus,
    QueueStatusService,
    QueueStatusUnavailable,
    SocketQueueStatusReader,
    build_queue_status_tools,
    queue_status_mcp_descriptor,
)
from vs_evaluation.api.testing import (
    FakeQueueStatusReader,
    QueueStatusReaderContract,
    QueueStatusReaderUnderTest,
)
from vs_mcp.api import tool_server
from vs_sim.api.testing import HANG_GUARD_S, ManualClock, SimNetwork, SimThreads, examples

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_evaluation.api.queue_status import QueueStatusReader
    from vs_sim.api import Connection

_SOCKET = Path("/run/queue-status.sock")
_seconds = st.floats(min_value=0, max_value=1e6, allow_nan=False)
_counts = st.integers(min_value=0, max_value=10_000)


@st.composite
def _items(draw: st.DrawFn) -> QueueItem:
    state = draw(st.sampled_from(QueueItemState))
    waiting = state is QueueItemState.WAITING
    return QueueItem(
        id=draw(st.from_regex(r"[0-9]{1,9}", fullmatch=True)),
        kind=draw(st.sampled_from(QueueItemKind)),
        state=state,
        waited_seconds=draw(st.none() | _seconds),
        running_seconds=None if waiting else draw(st.none() | _seconds),
        jobs_ahead=draw(st.none() | _counts) if waiting else None,
        estimated_start_in_seconds=draw(st.none() | _seconds) if waiting else None,
        wait_reason=draw(st.none() | st.sampled_from(["Priority", "Resources"]))
        if waiting
        else None,
    )


@st.composite
def _contention(draw: st.DrawFn) -> QueueContention:
    total = draw(st.none() | _counts)
    busy = None if total is None else draw(st.integers(0, total))
    return QueueContention(
        jobs_waiting=draw(st.none() | _counts),
        jobs_running=draw(st.none() | _counts),
        gpus_total=total,
        gpus_busy=busy,
        gpus_free=None if total is None or busy is None else total - busy,
    )


_statuses = st.builds(
    QueueStatus,
    queue=st.sampled_from(QueueKind),
    items=st.none() | st.lists(_items(), max_size=4).map(tuple),
    contention=_contention(),
)
_readings: st.SearchStrategy[QueueStatus | QueueStatusUnavailable] = _statuses | st.builds(
    QueueStatusUnavailable.because, st.text(min_size=1, max_size=800)
)


def _reading(seed: int) -> QueueStatus | QueueStatusUnavailable:
    """A reading chosen by seed, without Hypothesis, for the contract's seeded cases."""
    if seed % 4 == 3:
        return QueueStatusUnavailable.because(f"backend {seed} is down")
    items = tuple(
        QueueItem(
            id=str(100 + index),
            kind=QueueItemKind.EVALUATION if index % 2 else QueueItemKind.AGENT_GPU,
            state=QueueItemState.WAITING,
            waited_seconds=float(60 * index),
            jobs_ahead=index,
        )
        for index in range(seed % 3)
    )
    busy = seed % 9
    return QueueStatus(
        queue=QueueKind.SHARED,
        items=items,
        contention=QueueContention(
            jobs_waiting=seed, gpus_total=8, gpus_busy=busy, gpus_free=8 - busy
        ),
    )


# ---------------------------------------------------------------------------
# Every implementation passes the one contract suite.
# ---------------------------------------------------------------------------


class TestFakeReader(QueueStatusReaderContract):
    def reader_under_test(self, seed: int) -> QueueStatusReaderUnderTest:
        reading = _reading(seed)
        return QueueStatusReaderUnderTest(FakeQueueStatusReader([reading]), expected=reading)


class TestLocalReader(QueueStatusReaderContract):
    def reader_under_test(self, seed: int) -> QueueStatusReaderUnderTest:
        del seed
        return QueueStatusReaderUnderTest(LocalQueueStatusReader())


class TestOpaqueReader(QueueStatusReaderContract):
    def reader_under_test(self, seed: int) -> QueueStatusReaderUnderTest:
        del seed
        return QueueStatusReaderUnderTest(OpaqueQueueStatusReader())


class TestCachedReader(QueueStatusReaderContract):
    def reader_under_test(self, seed: int) -> QueueStatusReaderUnderTest:
        reading = _reading(seed)
        threads = SimThreads(schedule_seed=seed)
        reader = CachedQueueStatusReader(
            FakeQueueStatusReader([reading]),
            clock=ManualClock(),
            threads=threads,
            fresh_for_s=FRESH_FOR_S,
        )
        return QueueStatusReaderUnderTest(reader, run=threads.run, expected=reading)


class TestSocketReader(QueueStatusReaderContract):
    """The agent-side reader returns what the host's service reads, over the socket."""

    def reader_under_test(self, seed: int) -> QueueStatusReaderUnderTest:
        reading = _reading(seed)
        threads = SimThreads(schedule_seed=seed)
        network = SimNetwork(threads)
        backend = FakeQueueStatusReader([reading])
        reader = SocketQueueStatusReader(_SOCKET, network=network, clock=threads)

        def run(call: Callable[[], object]) -> object:
            # A closed service stays closed, so each call is served by a fresh one.
            service = QueueStatusService(
                backend, _SOCKET, network=network, threads=threads, clock=threads
            )

            def main() -> object:
                service.start()
                try:
                    return call()
                finally:
                    service.close()

            return threads.run(main)

        return QueueStatusReaderUnderTest(reader, run=run, expected=reading)


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------


@given(reading=_readings)
def test_every_reading_survives_the_wire(reading: QueueStatus | QueueStatusUnavailable) -> None:
    assert QUEUE_READING.validate_json(reading.model_dump_json()) == reading


@given(text=st.text(max_size=2000))
def test_an_unavailable_reason_is_never_empty_and_never_too_long(text: str) -> None:
    reason = QueueStatusUnavailable.because(text).reason
    assert 1 <= len(reason) <= 500


@given(seconds=_seconds, ahead=_counts)
def test_a_running_item_has_no_position_and_a_waiting_one_no_running_time(
    seconds: float, ahead: int
) -> None:
    with pytest.raises(ValidationError):
        QueueItem(
            id="1",
            kind=QueueItemKind.EVALUATION,
            state=QueueItemState.RUNNING,
            jobs_ahead=ahead,
        )
    with pytest.raises(ValidationError):
        QueueItem(
            id="1",
            kind=QueueItemKind.EVALUATION,
            state=QueueItemState.WAITING,
            running_seconds=seconds,
        )


@given(total=_counts, busy=_counts, free=_counts)
def test_gpu_counts_that_do_not_add_up_are_refused(total: int, busy: int, free: int) -> None:
    if busy + free == total:
        assert QueueContention(gpus_total=total, gpus_busy=busy, gpus_free=free).gpus_free == free
    else:
        with pytest.raises(ValidationError):
            QueueContention(gpus_total=total, gpus_busy=busy, gpus_free=free)


def test_an_unknown_key_in_a_reading_is_refused() -> None:
    document = json.loads(QueueStatus(queue=QueueKind.LOCAL, items=None).model_dump_json())
    document["user"] = "someone"
    with pytest.raises(ValidationError):
        QUEUE_READING.validate_python(document)


# ---------------------------------------------------------------------------
# The cache: many agents cost one backend read
# ---------------------------------------------------------------------------


@settings(max_examples=examples(pr=20, nightly=300), deadline=None)
@given(gaps=st.lists(st.floats(min_value=0, max_value=3 * FRESH_FOR_S), max_size=12))
def test_the_backend_is_read_once_per_freshness_window(gaps: list[float]) -> None:
    readings = [_reading(seed) for seed in range(4)]
    backend = FakeQueueStatusReader(list(readings))
    clock = ManualClock()
    threads = SimThreads(schedule_seed=0)
    reader = CachedQueueStatusReader(backend, clock=clock, threads=threads, fresh_for_s=FRESH_FOR_S)

    def main() -> None:
        last_read_at = clock.now()
        expected_reads = 1
        assert reader.read() == readings[0]
        for gap in gaps:
            clock.advance(gap)
            if clock.now() - last_read_at >= FRESH_FOR_S:
                expected_reads += 1
                last_read_at = clock.now()
            reader.read()
            assert backend.reads == expected_reads

    threads.run(main)


# ---------------------------------------------------------------------------
# The agent tool, through synthesized tool calls
# ---------------------------------------------------------------------------


async def _call(reader: QueueStatusReader, arguments: dict[str, object]) -> str:
    server = tool_server(build_queue_status_tools(reader))
    _, structured = cast(
        "tuple[object, dict[str, Any]]", await server.call_tool(READ_QUEUE_STATUS_TOOL, arguments)
    )
    return structured["result"]


@settings(max_examples=examples(pr=20, nightly=300), deadline=None)
@given(reading=_readings)
def test_the_tool_returns_the_reading_as_json(
    reading: QueueStatus | QueueStatusUnavailable,
) -> None:
    result = asyncio.run(_call(FakeQueueStatusReader([reading]), {}))
    assert QUEUE_READING.validate_json(result) == reading


@given(
    arguments=st.dictionaries(
        st.text(min_size=1, max_size=12),
        st.integers() | st.text(max_size=8),
        min_size=1,
        max_size=3,
    )
)
def test_the_tool_refuses_every_argument(arguments: dict[str, object]) -> None:
    backend = FakeQueueStatusReader([_reading(0)])
    with pytest.raises(ToolError):
        asyncio.run(_call(backend, arguments))
    assert backend.reads == 0


def test_the_server_offers_only_the_read_tool() -> None:
    server = tool_server(build_queue_status_tools(LocalQueueStatusReader()))
    tools = asyncio.run(server.list_tools())
    assert [tool.name for tool in tools] == [READ_QUEUE_STATUS_TOOL]
    assert tools[0].inputSchema.get("additionalProperties") is False
    assert not tools[0].inputSchema.get("properties")


def test_the_descriptor_carries_the_socket_as_a_launch_only_endpoint() -> None:
    descriptor = queue_status_mcp_descriptor("/run/vse-x/queue-status.sock")
    assert descriptor.command == "python"
    assert descriptor.args == ("-m", "vs_evaluation.queue_status_mcp")
    assert descriptor.env == ()
    assert dict(descriptor.runtime_env) == {
        "VS_QUEUE_STATUS_SOCKET": "/run/vse-x/queue-status.sock"
    }


# ---------------------------------------------------------------------------
# The wire: a host that is gone, slow or broken is an unavailable reading
# ---------------------------------------------------------------------------

_TIMEOUT_S = 10.0


def _read_against(answer: Callable[[Connection, SimThreads], None] | None) -> tuple[object, float]:
    """One socket read against a scripted host (``None``: no host listens); outcome and duration."""
    threads = SimThreads(schedule_seed=0)

    def main() -> tuple[object, float]:
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
        reader = SocketQueueStatusReader(
            _SOCKET, network=network, clock=threads, timeout_s=_TIMEOUT_S
        )
        started = threads.now()
        try:
            return reader.read(), threads.now() - started
        finally:
            if listener is not None:
                listener.close()

    return threads.run(main)


_GARBAGE = st.one_of(
    st.binary(max_size=64).map(lambda raw: raw.replace(b"\n", b"")),
    st.sampled_from([b"{}", b"[]", b"null", b'{"status": "read"}', b'{"status": "gone"}']),
)


@settings(max_examples=examples(pr=20, nightly=500), deadline=None)
@given(line=_GARBAGE)
def test_a_malformed_reply_is_an_unavailable_reading(line: bytes) -> None:
    def answer(connection: Connection, _threads: SimThreads) -> None:
        connection.recv(4096, HANG_GUARD_S)
        connection.send(line + b"\n")

    outcome, _ = _read_against(answer)
    assert isinstance(outcome, QueueStatusUnavailable), repr(outcome)


def test_no_host_is_an_unavailable_reading() -> None:
    outcome, _ = _read_against(None)
    assert isinstance(outcome, QueueStatusUnavailable)
    assert "unreachable" in outcome.reason


@settings(max_examples=examples(pr=10, nightly=200), deadline=None)
@given(interval_s=st.floats(min_value=0.01, max_value=_TIMEOUT_S * 0.99))
def test_a_trickling_host_cannot_outlast_the_call_deadline(interval_s: float) -> None:
    def answer(connection: Connection, threads: SimThreads) -> None:
        connection.recv(4096, HANG_GUARD_S)
        for _ in range(100_000):
            connection.send(b" ")
            threads.sleep(interval_s)

    outcome, elapsed = _read_against(answer)
    assert isinstance(outcome, QueueStatusUnavailable)
    assert elapsed <= _TIMEOUT_S + interval_s


def test_a_malformed_call_gets_an_unavailable_reading_and_never_reads_the_backend() -> None:
    threads = SimThreads(schedule_seed=0)
    backend = FakeQueueStatusReader([_reading(0)])

    def main() -> bytes:
        network = SimNetwork(threads)
        service = QueueStatusService(
            backend, _SOCKET, network=network, threads=threads, clock=threads
        )
        service.start()
        try:
            connection = network.connect(str(_SOCKET), HANG_GUARD_S)
            connection.send(b'{"action": "cancel", "job": "42"}\n')
            reply = b""
            while not reply.endswith(b"\n"):
                chunk = connection.recv(65536, HANG_GUARD_S)
                if not chunk:
                    break
                reply += chunk
            connection.close()
            return reply
        finally:
            service.close()

    reply = threads.run(main)
    assert isinstance(QUEUE_READING.validate_json(reply), QueueStatusUnavailable)
    assert backend.reads == 0
