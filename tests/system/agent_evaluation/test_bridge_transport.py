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
from pathlib import Path
from typing import TYPE_CHECKING, cast

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter
from tests.support.skeleton_strategy import DIGEST
from tests.support.synthesized_evidence import SynthesizedEvidence

from vs_core.api import ArtifactId, ArtifactRef
from vs_evaluation.api import SocketFailure, SocketSuccess, SubmitCall
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
from vs_sim.api.testing import (
    HANG_GUARD_S,
    SimBlockingRunner,
    SimNetwork,
    SimThreads,
    VirtualClock,
    examples,
    hostile_json_frames,
    run_virtual,
)

if TYPE_CHECKING:
    import pytest

    from vs_runtime.api.infrastructure import RuntimeWorkspaces
    from vs_sim.api import Connection

ADDRESS = "evaluation.sock"
REPLY = TypeAdapter(SocketSuccess | SocketFailure)
_FOREIGN_CALL = SubmitCall.model_validate({"token": "not-a-token"}).model_dump_json().encode()
_FRAMES = hostile_json_frames((_FOREIGN_CALL,))
_LARGEST_FRAME = 1 << 16
"""The bridge's line bound; only a frame past it may go unanswered."""


def _bridge(root: Path, network: SimNetwork, threads: SimThreads) -> AgentEvaluationBridge:
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
    return AgentEvaluationBridge(
        Path(ADDRESS),
        policy,
        ScopeWorkspaces(
            cast("RuntimeWorkspaces", FakeWorkspaces(FakeWorkspace(path=Path("/workspace")))),
            StoreWorkspaceReceipts(ReceiptStore(namespace, threads=SimThreads())),
        ),
        AgentEvaluationRecords(
            OwedSubmissions(ReceiptStore(namespace, threads=SimThreads())), SynthesizedEvidence()
        ),
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
