"""The evaluation bridge's socket service holds up against clients that misbehave.

The bridge serves over ``SimNetwork`` on ``SimThreads`` inside the virtual event loop, so
a schedule is a seed and every wait is virtual. A client may send nothing, hang up
mid-frame, send a frame past the size bound or not JSON at all, or read its reply and
then neither confirm nor hang up. Every such call is answered with a typed refusal or
dropped, the next call is still served, and ``close`` returns instead of waiting on the
client.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter
from tests.support.skeleton_strategy import DIGEST
from tests.support.synthesized_evidence import SynthesizedEvidence

from vs_core.api import ArtifactId, ArtifactRef
from vs_evaluation.api import RECEIPT_LINE, SocketFailure, SocketSuccess, SubmitCall
from vs_project.api import Project
from vs_runtime.api.core import (
    AgentEvaluationBridge,
    AgentEvaluationPolicy,
    AgentEvaluationRecords,
    OwedSubmissions,
    ReceiptStore,
    ScopeWorkspaces,
    StoreWorkspaceReceipts,
)
from vs_runtime.api.testing import FakeWorkspace, FakeWorkspaces
from vs_sim.api import WorkerStartRefusedError, yield_now
from vs_sim.api.testing import (
    HANG_GUARD_S,
    RefusingThreads,
    SimBlockingRunner,
    SimNetwork,
    SimThreads,
    VirtualClock,
    examples,
    hostile_json_frames,
    run_virtual,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from contextvars import Context
    from typing import Any

    from vs_runtime.api.infrastructure import RuntimeWorkspaces
    from vs_sim.api import (
        Condition,
        Connection,
        Event,
        Listener,
        Lock,
        Network,
        ThreadLocal,
        Threads,
        Worker,
    )

ADDRESS = "evaluation.sock"
REPLY = TypeAdapter(SocketSuccess | SocketFailure)
_FOREIGN_CALL = SubmitCall.model_validate({"token": "not-a-token"}).model_dump_json().encode()
_FRAMES = hostile_json_frames((_FOREIGN_CALL,))
_LARGEST_FRAME = 1 << 16
"""The bridge's line bound; only a frame past it may go unanswered."""
_UNBOUNDED_ANSWER_RETURNED = "an unbounded answer must be cancelled by bridge close"
_RECEIPT_STORE_UNAVAILABLE = "receipt store unavailable"


class _BlockingAnswerBridge(AgentEvaluationBridge):
    """A bridge whose public answer boundary exposes deterministic ownership."""

    def arm(self) -> None:
        """Prepare one answer to remain live until released or cancelled."""
        self.answer_started = asyncio.Event()
        self.release_answer = asyncio.Event()
        self.answer_cancelled = False
        self.answer_finished = False

    async def answer(self, frame: bytes) -> tuple[bytes, Callable[[], None]]:
        """Hold one call inside bridge-owned event-loop work."""
        self.answer_started.set()
        try:
            await self.release_answer.wait()
        except asyncio.CancelledError:
            self.answer_cancelled = True
            raise
        self.answer_finished = True
        return await super().answer(frame)


class _RecordingAcknowledgementBridge(AgentEvaluationBridge):
    """A bridge that records whether its receipt effect ran after close returned."""

    def arm(self) -> None:
        """Prepare the close boundary observed by one acknowledgement."""
        self.close_returned = False
        self.acknowledged_after_close: list[bool] = []

    async def answer(self, frame: bytes) -> tuple[bytes, Callable[[], None]]:
        """Answer without run resources and expose the receipt effect."""
        del frame

        def acknowledge() -> None:
            self.acknowledged_after_close.append(self.close_returned)

        return b"answer", acknowledge


class _FailingAcknowledgementBridge(AgentEvaluationBridge):
    """A bridge whose receipt-store effect exposes durable-store failure ownership."""

    def arm(self, acknowledgement_started: Event) -> None:
        """Expose the instant immediately before the receipt-store failure."""
        self.acknowledgement_started = acknowledgement_started

    async def answer(self, frame: bytes) -> tuple[bytes, Callable[[], None]]:
        """Answer successfully, then fail while durably recording its receipt."""
        del frame

        def acknowledge() -> None:
            self.acknowledgement_started.set()
            raise OSError(_RECEIPT_STORE_UNAVAILABLE)

        return b"answer", acknowledge


class _StartObservedBridge(AgentEvaluationBridge):
    """A bridge that records whether queued answer work entered its coroutine body."""

    def arm(self) -> None:
        """Prepare one answer that remains active until close cancels it."""
        self.answer_started = False

    async def answer(self, frame: bytes) -> tuple[bytes, Callable[[], None]]:
        """Record task start, then remain live for bridge shutdown."""
        del frame
        self.answer_started = True
        await asyncio.Event().wait()
        raise AssertionError(_UNBOUNDED_ANSWER_RETURNED)


class _WaitObservedEvent:
    """An event whose blocking wait announces that its owner has published work."""

    def __init__(self, inner: Event, wait_started: Event) -> None:
        self._inner = inner
        self._wait_started = wait_started

    def is_set(self) -> bool:
        """Whether the underlying event is set."""
        return self._inner.is_set()

    def set(self) -> None:
        """Set the underlying event."""
        self._inner.set()

    def clear(self) -> None:
        """Clear the underlying event."""
        self._inner.clear()

    def wait(self, timeout: float | None = None) -> bool:
        """Announce the wait before blocking on the underlying event."""
        self._wait_started.set()
        return self._inner.wait(timeout)


class _CallWaitObservingThreads:
    """Thread adapter that exposes when a loop-call worker begins waiting."""

    def __init__(self, inner: SimThreads) -> None:
        self._inner = inner
        self.call_waiting = inner.event()

    def spawn(self, target: Callable[[], object], *, name: str, daemon: bool = True) -> Worker:
        """Delegate worker creation."""
        return self._inner.spawn(target, name=name, daemon=daemon)

    def lock(self) -> Lock:
        """Delegate lock creation."""
        return self._inner.lock()

    def rlock(self) -> Lock:
        """Delegate reentrant-lock creation."""
        return self._inner.rlock()

    def condition(self, lock: Lock | None = None) -> Condition:
        """Delegate condition creation."""
        return self._inner.condition(lock)

    def sleep(self, seconds: float) -> None:
        """Delegate virtual sleep."""
        self._inner.sleep(seconds)

    def event(self) -> Event:
        """Wrap each bridge loop-call event with a wait observation."""
        return _WaitObservedEvent(self._inner.event(), self.call_waiting)

    def local[T](self, initial: Callable[[], T]) -> ThreadLocal[T]:
        """Delegate thread-local creation."""
        return self._inner.local(initial)

    def now(self) -> float:
        """Return the delegated monotonic clock."""
        return self._inner.now()

    def run_async[T](self, main: Coroutine[object, object, T]) -> T:
        """Delegate hosted event-loop execution."""
        return self._inner.run_async(main)


class _ReceiptGateConnection:
    """A server connection paused after receiving the client's receipt."""

    def __init__(
        self,
        inner: Connection,
        receipt_read: Event,
        release_receipt: Event,
        worker_finished: Event,
    ) -> None:
        self._inner = inner
        self._receipt_read = receipt_read
        self._release_receipt = release_receipt
        self._worker_finished = worker_finished
        self._close_calls = 0

    def send(self, data: bytes, timeout: float | None = None) -> None:
        """Delegate one server write."""
        self._inner.send(data, timeout)

    def recv(self, max_bytes: int, timeout: float | None = None) -> bytes:
        """Pause after the receipt is read but before the bridge can acknowledge it."""
        data = self._inner.recv(max_bytes, timeout)
        if data == RECEIPT_LINE:
            self._receipt_read.set()
            self._release_receipt.wait(HANG_GUARD_S)
        return data

    def peer_closed(self) -> bool:
        """Delegate peer state."""
        return self._inner.peer_closed()

    def close(self) -> None:
        """Expose the worker's final close after bridge shutdown's first close."""
        self._close_calls += 1
        self._inner.close()
        if self._close_calls >= 2:
            self._worker_finished.set()


class _ReceiptGateListener:
    """Wrap the accepted server side in the receipt gate."""

    def __init__(
        self,
        inner: Listener,
        receipt_read: Event,
        release_receipt: Event,
        worker_finished: Event,
    ) -> None:
        self._inner = inner
        self._receipt_read = receipt_read
        self._release_receipt = release_receipt
        self._worker_finished = worker_finished

    def accept(self, timeout: float | None = None) -> Connection:
        """Accept one gated server connection."""
        return _ReceiptGateConnection(
            self._inner.accept(timeout),
            self._receipt_read,
            self._release_receipt,
            self._worker_finished,
        )

    def close(self) -> None:
        """Close the underlying listener."""
        self._inner.close()


class _ReceiptGateNetwork:
    """Network adapter that gates only the server's receipt read."""

    def __init__(
        self,
        inner: Network,
        receipt_read: Event,
        release_receipt: Event,
        worker_finished: Event,
    ) -> None:
        self._inner = inner
        self._receipt_read = receipt_read
        self._release_receipt = release_receipt
        self._worker_finished = worker_finished

    def listen(self, address: str) -> Listener:
        """Listen through a receipt-gated server adapter."""
        return _ReceiptGateListener(
            self._inner.listen(address),
            self._receipt_read,
            self._release_receipt,
            self._worker_finished,
        )

    def connect(self, address: str, timeout: float | None = None) -> Connection:
        """Connect the client without a gate."""
        return self._inner.connect(address, timeout)


def _bridge(
    root: Path,
    network: Network,
    threads: Threads,
    diagnostics: Callable[[str], None] | None = None,
    bridge_type: type[AgentEvaluationBridge] = AgentEvaluationBridge,
) -> AgentEvaluationBridge:
    namespace = Project.open(root).state.state_store_namespace("transport")
    policy = AgentEvaluationPolicy(
        evaluator_digest=DIGEST,
        workload_digest=DIGEST,
        environment_digest=DIGEST,
        recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest=DIGEST),
        stages=(("accuracy", 10.0),),
        queue_allowance=880.0,
        accuracy_stage="accuracy",
        accuracy_gated=(),
    )
    return bridge_type(
        Path(ADDRESS),
        policy,
        ScopeWorkspaces(
            cast("RuntimeWorkspaces", FakeWorkspaces(FakeWorkspace(path=Path("/workspace")))),
            StoreWorkspaceReceipts(ReceiptStore(namespace, threads=threads)),
        ),
        AgentEvaluationRecords(
            OwedSubmissions(ReceiptStore(namespace, threads=threads)), SynthesizedEvidence()
        ),
        diagnostics=diagnostics or (lambda _message: None),
        network=network,
        threads=threads,
    )


def _reply_line(connection: Connection) -> bytes:
    """The reply line, or what came before the bridge hung up (empty: no reply)."""
    received = bytearray()
    while b"\n" not in received and (chunk := connection.recv(1 << 16, HANG_GUARD_S)):
        received += chunk
    return bytes(received)


def _call(network: SimNetwork, frame: bytes) -> bytes:
    """Send one frame, read the reply line, and hang up without a receipt."""
    connection = network.connect(ADDRESS, HANG_GUARD_S)
    try:
        # The bridge reads up to its frame bound and hangs up while the rest of an
        # oversized frame waits for room, as it would on a real socket.
        with contextlib.suppress(BrokenPipeError):
            connection.send(frame, HANG_GUARD_S)
        return _reply_line(connection)
    finally:
        connection.close()


def test_a_refused_call_worker_drops_only_that_connection(tmp_path: Path) -> None:
    """One transient worker-start refusal does not kill the bridge's accept loop."""
    clock = VirtualClock()
    simulator = SimThreads(clock)
    threads = RefusingThreads(simulator, {"agent-evaluation-call": 1})
    network = SimNetwork(simulator)
    diagnostics: list[str] = []
    bridge = _bridge(tmp_path, network, threads, diagnostics.append)
    runner = SimBlockingRunner(simulator)

    def open_call() -> Connection:
        connection = network.connect(ADDRESS, HANG_GUARD_S)
        connection.send(_FOREIGN_CALL + b"\n", HANG_GUARD_S)
        return connection

    def finish_call(connection: Connection) -> bytes:
        try:
            return _reply_line(connection)
        finally:
            connection.close()

    async def main() -> tuple[bytes, bytes]:
        await bridge.serve()
        try:
            first = await runner.run(open_call)
            refused = await runner.run(finish_call, first)
            served = await runner.run(_call, network, _FOREIGN_CALL + b"\n")
            return refused, served
        finally:
            await bridge.close()

    refused, served = simulator.run(lambda: simulator.run_async(main()))

    assert refused == b""
    assert isinstance(REPLY.validate_json(served), SocketFailure)
    assert any("call worker did not start" in line for line in diagnostics)


def test_a_refused_accept_worker_releases_the_listener_address(tmp_path: Path) -> None:
    """A bridge that cannot start its acceptor leaves no partially owned listener behind."""
    clock = VirtualClock()
    simulator = SimThreads(clock)
    network = SimNetwork(simulator)
    bridge = _bridge(
        tmp_path,
        network,
        RefusingThreads(simulator, {"agent-evaluation-bridge": 1}),
    )

    async def main() -> None:
        with pytest.raises(WorkerStartRefusedError, match="agent-evaluation-bridge"):
            await bridge.serve()
        replacement = network.listen(ADDRESS)
        replacement.close()
        await bridge.close()

    simulator.run(lambda: simulator.run_async(main()))


def test_close_cancels_and_drains_a_call_already_owned_by_the_loop(tmp_path: Path) -> None:
    """No admitted evaluation work may use run resources after bridge shutdown."""
    clock = VirtualClock()
    threads = SimThreads(clock)
    network = SimNetwork(threads)
    bridge = cast(
        "_BlockingAnswerBridge",
        _bridge(tmp_path, network, threads, bridge_type=_BlockingAnswerBridge),
    )
    bridge.arm()
    runner = SimBlockingRunner(threads)

    def open_call() -> Connection:
        connection = network.connect(ADDRESS, HANG_GUARD_S)
        connection.send(_FOREIGN_CALL + b"\n", HANG_GUARD_S)
        return connection

    def finish_call(connection: Connection) -> bytes:
        try:
            return _reply_line(connection)
        finally:
            connection.close()

    async def main() -> tuple[bool, bool, bytes]:
        await bridge.serve()
        connection = await runner.run(open_call)
        reply = asyncio.create_task(runner.run(finish_call, connection))
        await bridge.answer_started.wait()
        await bridge.close()
        cancelled_at_close = bridge.answer_cancelled
        finished_at_close = bridge.answer_finished
        # On the regression base this is what lets the unowned answer finally
        # settle, keeping the failing test itself deterministic and leak-free.
        bridge.release_answer.set()
        return cancelled_at_close, finished_at_close, await reply

    cancelled, finished, reply = run_virtual(clock, main(), driver=threads)

    assert cancelled
    assert not finished
    assert reply == b""


def test_close_cancels_a_published_call_before_its_start_callback_runs(tmp_path: Path) -> None:
    """A queued call is woken and its coroutine closed without ever starting."""

    def run(seed: int) -> tuple[bool, bytes]:
        root = tmp_path / str(seed)
        root.mkdir()
        clock = VirtualClock()
        threads = SimThreads(clock, schedule_seed=seed)
        observed_threads = _CallWaitObservingThreads(threads)
        network = SimNetwork(threads)
        bridge = cast(
            "_StartObservedBridge",
            _bridge(
                root,
                network,
                observed_threads,
                bridge_type=_StartObservedBridge,
            ),
        )
        bridge.arm()
        runner = SimBlockingRunner(threads)

        async def main() -> tuple[bool, bytes]:
            await bridge.serve()
            loop = asyncio.get_running_loop()
            closed = asyncio.Event()

            async def close_bridge() -> None:
                await bridge.close()
                closed.set()

            def request_close() -> None:
                assert observed_threads.call_waiting.wait(HANG_GUARD_S)
                loop.call_soon_threadsafe(lambda: asyncio.create_task(close_bridge()))

            threads.spawn(request_close, name="close-queued-call")
            client_task = asyncio.create_task(runner.run(_call, network, b"call\n"))
            await closed.wait()
            return bridge.answer_started, await client_task

        return run_virtual(clock, main(), driver=threads, schedule_seed=seed)

    outcomes = tuple(run(seed) for seed in range(12))

    assert (False, b"") in outcomes
    assert all(reply == b"" for _, reply in outcomes)


def test_close_fences_a_receipt_read_before_its_acknowledgement(tmp_path: Path) -> None:
    """A receipt callback cannot mutate durable run state after bridge shutdown."""
    clock = VirtualClock()
    threads = SimThreads(clock)
    receipt_read = threads.event()
    release_receipt = threads.event()
    worker_finished = threads.event()
    network = _ReceiptGateNetwork(
        SimNetwork(threads), receipt_read, release_receipt, worker_finished
    )
    bridge = cast(
        "_RecordingAcknowledgementBridge",
        _bridge(tmp_path, network, threads, bridge_type=_RecordingAcknowledgementBridge),
    )
    bridge.arm()
    runner = SimBlockingRunner(threads)

    def client() -> bytes:
        connection = network.connect(ADDRESS, HANG_GUARD_S)
        try:
            connection.send(b"call\n", HANG_GUARD_S)
            reply = _reply_line(connection)
            connection.send(RECEIPT_LINE, HANG_GUARD_S)
            return reply
        finally:
            connection.close()

    async def main() -> tuple[bytes, tuple[bool, ...]]:
        await bridge.serve()
        client_task = asyncio.create_task(runner.run(client))
        assert await runner.run(receipt_read.wait, HANG_GUARD_S)
        await bridge.close()
        bridge.close_returned = True
        release_receipt.set()
        assert await runner.run(worker_finished.wait, HANG_GUARD_S)
        # Let a callback queued by the regression base run before the assertion.
        await yield_now()
        return await client_task, tuple(bridge.acknowledged_after_close)

    reply, acknowledgements = run_virtual(clock, main(), driver=threads)

    assert reply == b"answer\n"
    assert acknowledgements == ()


def test_a_receipt_store_failure_remains_a_worker_failure(tmp_path: Path) -> None:
    """A durable-store OSError must not be mistaken for a disconnected client."""
    clock = VirtualClock()
    threads = SimThreads(clock)
    network = SimNetwork(threads)
    acknowledgement_started = threads.event()
    bridge = cast(
        "_FailingAcknowledgementBridge",
        _bridge(
            tmp_path,
            network,
            threads,
            bridge_type=_FailingAcknowledgementBridge,
        ),
    )
    bridge.arm(acknowledgement_started)
    runner = SimBlockingRunner(threads)

    def client() -> bytes:
        connection = network.connect(ADDRESS, HANG_GUARD_S)
        try:
            connection.send(b"call\n", HANG_GUARD_S)
            reply = _reply_line(connection)
            connection.send(RECEIPT_LINE, HANG_GUARD_S)
            return reply
        finally:
            connection.close()

    async def main() -> bytes:
        await bridge.serve()
        try:
            reply = await runner.run(client)
            assert await runner.run(acknowledgement_started.wait, HANG_GUARD_S)
            for _ in range(16):
                if threads.errors:
                    break
                await yield_now()
            return reply
        finally:
            await bridge.close()

    reply = run_virtual(clock, main(), driver=threads)

    assert reply == b"answer\n"
    assert len(threads.errors) == 1
    assert isinstance(threads.errors[0], RuntimeError)
    assert str(threads.errors[0]) == "agent evaluation receipt acknowledgement failed"
    assert isinstance(threads.errors[0].__cause__, OSError)


def test_a_task_creation_failure_settles_the_published_call(tmp_path: Path) -> None:
    """A loop that rejects task ownership must close the work and wake its worker."""
    clock = VirtualClock()
    threads = SimThreads(clock)
    network = SimNetwork(threads)
    bridge = _bridge(tmp_path, network, threads)
    runner = SimBlockingRunner(threads)
    client_waiting = threads.event()
    release_client = threads.event()
    creation_failure = RuntimeError("task creation refused")
    rejected: list[Coroutine[Any, Any, Any]] = []

    def client() -> bytes:
        client_waiting.set()
        assert release_client.wait(HANG_GUARD_S)
        return _call(network, b"call\n")

    async def main() -> bytes:
        await bridge.serve()
        loop = asyncio.get_running_loop()
        client_task = asyncio.create_task(runner.run(client))
        assert await runner.run(client_waiting.wait, HANG_GUARD_S)
        original_factory = loop.get_task_factory()

        def fail_answer_creation(
            event_loop: asyncio.AbstractEventLoop,
            coroutine: Coroutine[Any, Any, Any],
            context: Context | None = None,
        ) -> asyncio.Future[Any]:
            if not rejected:
                rejected.append(coroutine)
                raise creation_failure
            if original_factory is not None:
                return original_factory(event_loop, coroutine)
            return asyncio.Task(coroutine, loop=event_loop, context=context)

        loop.set_task_factory(fail_answer_creation)
        try:
            release_client.set()
            return await client_task
        finally:
            loop.set_task_factory(original_factory)
            await bridge.close()

    reply = run_virtual(clock, main(), driver=threads)

    assert isinstance(REPLY.validate_json(reply), SocketFailure)
    assert threads.errors == [creation_failure]
    assert len(rejected) == 1
    assert inspect.getcoroutinestate(rejected[0]) == inspect.CORO_CLOSED


@settings(deadline=None, max_examples=examples(pr=8, nightly=300))
@given(
    frames=st.lists(st.sampled_from(_FRAMES), min_size=1, max_size=4),
    seed=st.integers(0, 2**32),
)
def test_hostile_frames_are_refused_or_dropped_and_the_next_call_is_served(
    tmp_path_factory: pytest.TempPathFactory, frames: list[tuple[str, bytes]], seed: int
) -> None:
    """Concurrent hostile clients each get a typed refusal, or none for an oversized frame."""
    clock = VirtualClock()
    threads = SimThreads(clock, schedule_seed=seed)
    network = SimNetwork(threads)
    bridge = _bridge(tmp_path_factory.mktemp("bridge"), network, threads)
    runner = SimBlockingRunner(threads)

    async def main() -> tuple[list[bytes], bytes]:
        await bridge.serve()
        try:
            answers = await asyncio.gather(
                *(runner.run(_call, network, frame + b"\n") for _, frame in frames)
            )
            return answers, await runner.run(_call, network, _FOREIGN_CALL + b"\n")
        finally:
            await bridge.close()

    answers, after = run_virtual(clock, main(), schedule_seed=seed, driver=threads)

    for (name, frame), answer in zip(frames, answers, strict=True):
        if answer:
            assert isinstance(REPLY.validate_json(answer), SocketFailure), name
        else:
            assert len(frame) > _LARGEST_FRAME, f"{name}: only an oversized frame goes unanswered"
    assert isinstance(REPLY.validate_json(after), SocketFailure)


@settings(deadline=None, max_examples=examples(pr=6, nightly=200))
@given(
    sent=st.sampled_from([b"", _FOREIGN_CALL[:10], _FOREIGN_CALL + b"\n"]),
    seed=st.integers(0, 2**32),
)
def test_close_hangs_up_on_a_client_that_never_finishes_its_call(
    tmp_path_factory: pytest.TempPathFactory, sent: bytes, seed: int
) -> None:
    """A client that holds its connection open cannot hold the run's shutdown.

    ``close`` used to await the asyncio server's ``wait_closed``, which waits for every
    connection to end, so a tool process that stayed connected and silent kept a finished
    run from returning.
    """
    clock = VirtualClock()
    threads = SimThreads(clock, schedule_seed=seed)
    network = SimNetwork(threads)
    bridge = _bridge(tmp_path_factory.mktemp("bridge"), network, threads)
    runner = SimBlockingRunner(threads)

    def hold() -> tuple[Connection, bytes]:
        connection = network.connect(ADDRESS, HANG_GUARD_S)
        connection.send(sent, HANG_GUARD_S)
        return connection, _reply_line(connection) if sent.endswith(b"\n") else b""

    def rest(connection: Connection) -> bytes:
        try:
            return _reply_line(connection)
        finally:
            connection.close()

    async def main() -> tuple[bytes, bytes]:
        await bridge.serve()
        connection, reply = await runner.run(hold)
        await bridge.close()
        return reply, await runner.run(rest, connection)

    reply, after_close = run_virtual(clock, main(), schedule_seed=seed, driver=threads)

    if sent.endswith(b"\n"):
        assert isinstance(REPLY.validate_json(reply), SocketFailure)
    assert after_close == b"", "the bridge hung up on the client when it closed"
