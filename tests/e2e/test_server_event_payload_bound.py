"""Real-socket regression for the recorded event payload bound."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from inspect import signature
from typing import TYPE_CHECKING, cast

from pydantic import TypeAdapter
from tests.server.support import REQUEST_TIME, build_server_parts
from websockets.asyncio.client import connect

from server.api.protocol import (
    ActiveExecutions,
    ActiveExecutionsChunkMessage,
    EventBatchMessage,
    ServerMessage,
    SubscribeRequest,
)
from server.events import MAX_SERIALIZED_RUN_EVENT_BYTES, EventType, OutputData, make_event
from server.transport.websocket import WebSocketGateway
from vs_sim.api import MonotonicClock, OsThreads, ThreadBlockingRunner
from vs_sim.api.testing import HANG_GUARD_S

if TYPE_CHECKING:
    from pathlib import Path

    from websockets.asyncio.client import ClientConnection
    from websockets.typing import Origin


_CLIENT_FRAME_CAP = cast("int", signature(connect).parameters["max_size"].default)


async def _next_frame(websocket: ClientConnection) -> str:
    """Read one frame with a deadline that only guards against a hang."""
    return cast("str", await asyncio.wait_for(websocket.recv(), timeout=HANG_GUARD_S))


def test_one_event_past_the_peer_frame_cap_arrives_cut_and_the_socket_stays_open(
    tmp_path: Path,
) -> None:
    """The recording contract, not a transport exception, bounds one event.

    Against the merge base the first ``event_batch`` is a message larger than
    the stock client's ``max_size``. The client closes it with code 1009 before
    any event arrives. The follow-up event proves delivery did not merely race
    a close: the same subscription receives another batch afterwards.
    """
    parts = build_server_parts(tmp_path / "logs")
    content = "x" * (_CLIENT_FRAME_CAP + 1024)
    produced = make_event(
        EventType.OUTPUT,
        data=OutputData(stream="stdout", content=content),
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )
    stamped = produced.model_copy(
        update={"sequence": parts.api.latest_sequence + 1, "run_id": parts.journal.run_id_locked()}
    )
    assert len(stamped.model_dump_json().encode()) > _CLIENT_FRAME_CAP
    recorded = parts.journal.append(produced)
    assert recorded.truncated is True
    assert len(recorded.model_dump_json().encode()) <= MAX_SERIALIZED_RUN_EVENT_BYTES

    async def receive(
        gateway: WebSocketGateway,
    ) -> tuple[str, EventBatchMessage, EventBatchMessage]:
        origin = f"http://127.0.0.1:{gateway.bound_port}"
        async with connect(gateway.websocket_url, origin=cast("Origin", origin)) as websocket:
            await websocket.send(
                SubscribeRequest(
                    timestamp=REQUEST_TIME, client_id="large-event-client"
                ).model_dump_json()
            )
            assert json.loads(await _next_frame(websocket))["type"] == "subscribed"
            first_frame = await _next_frame(websocket)
            first = EventBatchMessage.model_validate_json(first_frame)
            parts.journal.publish_output("stdout", "still connected")
            second = EventBatchMessage.model_validate_json(await _next_frame(websocket))
            return first_frame, first, second

    with WebSocketGateway(
        parts.api,
        clock=MonotonicClock(),
        threads=OsThreads(),
        request_runner=ThreadBlockingRunner(),
    ) as gateway:
        first_frame, first, second = asyncio.run(receive(gateway))
    parts.close()

    assert len(first_frame.encode()) <= _CLIENT_FRAME_CAP
    delivered = next(event for event in first.events if event.sequence == recorded.sequence)
    assert delivered == recorded
    assert delivered.truncated is True
    assert isinstance(delivered.data, OutputData)
    assert content.startswith(delivered.data.content)
    assert any(
        event.text == "" and event.data == OutputData(stream="stdout", content="still connected")
        for event in second.events
    )


def test_stock_websocket_client_receives_an_oversized_active_checkpoint_in_bounded_frames(
    tmp_path: Path,
) -> None:
    """The public gateway keeps its active checkpoint below the peer's real cap."""
    parts = build_server_parts(tmp_path / "logs")
    assignment = "x" * (_CLIENT_FRAME_CAP + 64 * 1024)
    execution = parts.start_execution("implementer", "round-1", assignment)

    async def receive(gateway: WebSocketGateway) -> tuple[list[str], list[object]]:
        origin = f"http://127.0.0.1:{gateway.bound_port}"
        frames: list[str] = []
        messages: list[object] = []
        async with connect(gateway.websocket_url, origin=cast("Origin", origin)) as websocket:
            await websocket.send(
                SubscribeRequest(
                    timestamp=REQUEST_TIME, client_id="large-checkpoint-client"
                ).model_dump_json()
            )
            assert json.loads(await _next_frame(websocket))["type"] == "subscribed"
            first_frame = await _next_frame(websocket)
            frames.append(first_frame)
            first = TypeAdapter(ServerMessage).validate_json(first_frame)
            messages.append(first)
            assert isinstance(first, EventBatchMessage)
            assert first.active_executions is None
            while True:
                frame = await _next_frame(websocket)
                frames.append(frame)
                message = TypeAdapter(ServerMessage).validate_json(frame)
                messages.append(message)
                if isinstance(message, ActiveExecutionsChunkMessage) and (
                    message.chunk_index + 1 == message.chunk_count
                ):
                    return frames, messages

    with WebSocketGateway(
        parts.api,
        clock=MonotonicClock(),
        threads=OsThreads(),
        request_runner=ThreadBlockingRunner(),
    ) as gateway:
        frames, messages = asyncio.run(receive(gateway))
    parts.close()

    assert all(len(frame.encode()) <= _CLIENT_FRAME_CAP for frame in frames)
    chunks = [message for message in messages if isinstance(message, ActiveExecutionsChunkMessage)]
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    assert {chunk.chunk_count for chunk in chunks} == {len(chunks)}
    active = TypeAdapter(ActiveExecutions).validate_json("".join(chunk.payload for chunk in chunks))
    assert [(item.execution_id, item.assignment) for item in active] == [
        (execution.execution_id, assignment)
    ]
