"""The engine over hand-written Claude stream-json frames."""

from __future__ import annotations

from typing import Any

from tests import helpers as h
from vs_sidecar.engine import Engine
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch
from vs_sidecar.store import FrameDirection, MemoryStore

SESSION = "11111111-1111-4111-8111-111111111111"
INIT: dict[str, Any] = {"type": "system", "subtype": "init", "session_id": SESSION, "model": "m"}
RESULT: dict[str, Any] = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "done",
    "num_turns": 1,
    "session_id": SESSION,
    "usage": {"input_tokens": 3, "output_tokens": 2},
    "total_cost_usd": 0.01,
}


def _assistant(block: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"id": "msg_1", "role": "assistant", "model": "m", "content": [block]},
        "parent_tool_use_id": None,
        "session_id": SESSION,
    }


def _partial(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "stream_event",
        "event": event,
        "parent_tool_use_id": None,
        "session_id": SESSION,
    }


FRAMES = [
    INIT,
    _partial({"type": "message_start", "message": {"id": "msg_1"}}),
    _partial(
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hel"}}
    ),
    _partial(
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "lo"}}
    ),
    _assistant({"type": "text", "text": "hello"}),
    RESULT,
]


def _engine() -> tuple[Engine, MemoryStore]:
    store = MemoryStore()
    engine = Engine(h.launch(orch.HARNESS_CLAUDE_CODE), store, h.claude_factory([FRAMES]))
    engine.start()
    return engine, store


def test_claude_names_its_conversation_before_the_first_turn() -> None:
    _, store = _engine()
    (opened,) = store.events_after(0)
    assert opened.opened.provider_conversation_id == SESSION


def test_streamed_text_names_the_item_the_completed_text_arrives_as() -> None:
    engine, store = _engine()
    engine.send(h.start_turn("e1", "t1"))
    engine.run_next()
    events = store.events_after(0)
    deltas = [e.delta for e in events if e.HasField("delta")]
    assert [(d.item_id, d.text) for d in deltas] == [("msg_1:0", "hel"), ("msg_1:0", "lo")]
    (item,) = [e.item for e in events if e.HasField("item")]
    assert (item.id, item.assistant.text, item.status) == (
        "msg_1:0",
        "hello",
        orch.ITEM_STATUS_FINISHED,
    )
    assert events[-1].turn_ended.status == orch.TURN_STATUS_COMPLETED


def test_the_raw_log_has_the_frames_in_both_directions() -> None:
    engine, store = _engine()
    engine.send(h.start_turn("e1", "t1", "hello there"))
    engine.run_next()
    frames = store.raw_frames()
    assert {f.direction for f in frames} == {
        FrameDirection.TO_PROVIDER,
        FrameDirection.FROM_PROVIDER,
    }
    assert any("hello there" in f.text for f in frames if f.direction is FrameDirection.TO_PROVIDER)
    assert any(
        '"type":"result"' in f.text.replace(" ", "")
        for f in frames
        if f.direction is FrameDirection.FROM_PROVIDER
    )
    assert [f.after_offset for f in frames] == sorted(f.after_offset for f in frames)
