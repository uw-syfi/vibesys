"""Per-thread experiment chat routing, replay, and wire contracts."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.server.support import REQUEST_TIME, build_server_parts

from server.api.protocol import (
    ChatQuery,
    ChatThreadCreateQuery,
    ChatThreadInfo,
    Response,
)
from server.chat.manager import ChatAnswer, ChatThreadFactory, ChatThreadHandle
from server.events import ChatData, ChatThreadCreatedData, EventType, RunEvent, make_event
from server.host_api import HarnessChoice

_TIMESTAMP = datetime(2026, 1, 1, tzinfo=UTC)
OPUS = HarnessChoice(provider="claude", model="opus")


def _factory(calls: list[tuple[str, HarnessChoice | None]], answer: str) -> ChatThreadFactory:
    def factory(thread_id: str, harness: HarnessChoice | None) -> ChatThreadHandle:
        calls.append((thread_id, harness))
        return ChatThreadHandle(
            spec=ChatThreadCreatedData(
                thread_id=thread_id,
                provider="codex" if harness is None else harness.provider,
                model="gpt-default" if harness is None else harness.model,
                created_at=_TIMESTAMP,
            ),
            handler=lambda question: ChatAnswer(
                text=f"{answer}: {question}", invocation_id=f"exec-{thread_id}"
            ),
        )

    return factory


def _events(path: Path) -> list[dict]:
    return [
        json.loads(line) for line in (path / "run-events.jsonl").read_text().splitlines() if line
    ]


def test_created_thread_routes_chat_and_stamps_events(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    parts.chat.install_default_handler(
        lambda question: ChatAnswer(text=f"default: {question}", invocation_id="exec-default")
    )
    calls: list[tuple[str, HarnessChoice | None]] = []
    parts.chat.set_thread_factory(_factory(calls, "agentshim-claude"))

    spec = parts.chat.create_thread(harness=OPUS)
    assert calls == [(spec.thread_id, OPUS)]
    assert parts.chat.chat("what changed?", thread_id=spec.thread_id) == (
        "agentshim-claude: what changed?"
    )
    assert parts.chat.chat("what changed?") == "default: what changed?"

    created = [event for event in _events(tmp_path) if event["type"] == "chat_thread_created"]
    assert len(created) == 1
    assert created[0]["chat_thread_id"] == spec.thread_id
    assert created[0]["data"]["provider"] == "claude"
    assert created[0]["data"]["model"] == "opus"
    chats = [event for event in _events(tmp_path) if event["type"] == "chat"]
    assert [event["chat_thread_id"] for event in chats] == [spec.thread_id, None]
    assert chats[0]["agent_kind"] == "chat"


def test_chat_records_carry_the_answering_invocation_id(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    parts.chat.install_default_handler(
        lambda question: ChatAnswer(text=f"default: {question}", invocation_id="exec-default")
    )
    parts.chat.set_thread_factory(_factory([], "threaded"))
    spec = parts.chat.create_thread()

    parts.chat.chat("what changed?")
    parts.chat.chat("and here?", thread_id=spec.thread_id)

    chats = [event for event in _events(tmp_path) if event["type"] == "chat"]
    # Both routes stamp the terminal record with the invocation that produced
    # the answer, so clients fold it over exactly that streamed turn.
    assert [event["data"]["invocation_id"] for event in chats] == [
        "exec-default",
        f"exec-{spec.thread_id}",
    ]


def test_fallback_answers_take_fresh_invocation_identities(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)

    parts.chat.chat("what happened?")
    parts.chat.chat("still there?")

    chats = [event for event in _events(tmp_path) if event["type"] == "chat"]
    ids = [event["data"]["invocation_id"] for event in chats]
    # The read-only fallback never ran an agent, so each answer takes a fresh
    # identity: it must not claim a streamed turn some earlier invocation
    # opened and abandoned.
    assert all(ids)
    assert len(set(ids)) == len(ids) == 2


def test_unknown_thread_returns_clear_answer_without_event(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    answer = parts.chat.chat("hello?", thread_id="missing-thread")
    assert "Unknown experiment chat thread 'missing-thread'" in answer
    assert all(event["type"] != "chat" for event in _events(tmp_path))


def test_thread_creation_without_factory_is_rejected(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    with pytest.raises(RuntimeError, match="chat threads are not available"):
        parts.chat.create_thread()


def test_factory_validation_error_propagates_without_event(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)

    def rejecting_factory(*_args: object) -> ChatThreadHandle:
        _failure_message = "agent provider 'unknown' is not supported"
        raise ValueError(_failure_message)

    parts.chat.set_thread_factory(rejecting_factory)
    with pytest.raises(ValueError, match="provider 'unknown' is not supported"):
        parts.chat.create_thread(harness=HarnessChoice(provider="unknown", model="m"))
    assert all(event["type"] != "chat_thread_created" for event in _events(tmp_path))


def test_threads_replay_and_rebuild_on_demand(tmp_path: Path) -> None:
    first = build_server_parts(tmp_path)
    first.chat.set_thread_factory(_factory([], "first"))
    spec = first.chat.create_thread(harness=OPUS)
    assert first.chat.chat("why did round two regress so much?", thread_id=spec.thread_id)

    resumed = build_server_parts(tmp_path)
    replayed = resumed.chat.threads()
    assert [thread.thread_id for thread in replayed] == [spec.thread_id]
    assert replayed[0].title == "why did round two regress so much?"
    assert "cannot answer right now" in resumed.chat.chat("still there?", thread_id=spec.thread_id)

    calls: list[tuple[str, HarnessChoice | None]] = []
    resumed.chat.set_thread_factory(_factory(calls, "rebuilt"))
    assert resumed.chat.chat("still there?", thread_id=spec.thread_id) == ("rebuilt: still there?")
    assert calls == [(spec.thread_id, OPUS)]


def test_first_message_titles_an_untitled_thread_once(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    parts.chat.set_thread_factory(_factory([], "answer"))
    spec = parts.chat.create_thread()

    parts.chat.chat("explain why the benchmark throughput regressed in round four", spec.thread_id)
    parts.chat.chat("and round five?", thread_id=spec.thread_id)

    chats = [event for event in _events(tmp_path) if event["type"] == "chat"]
    assert chats[0]["data"]["thread_title"] == "explain why the benchmark throughput…"
    assert chats[1]["data"]["thread_title"] is None
    assert parts.chat.threads()[0].title == "explain why the benchmark throughput…"


def test_explicit_title_is_authoritative(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    parts.chat.set_thread_factory(_factory([], "answer"))
    spec = parts.chat.create_thread(title="  perf deep dive  ")
    parts.chat.chat("first question", thread_id=spec.thread_id)

    assert spec.title == "perf deep dive"
    chats = [event for event in _events(tmp_path) if event["type"] == "chat"]
    assert chats[0]["data"]["thread_title"] is None
    assert parts.chat.threads()[0].title == "perf deep dive"


def test_cleared_threads_stop_routing_but_keep_replayable_specs(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    parts.chat.set_thread_factory(_factory([], "answer"))
    spec = parts.chat.create_thread()
    parts.chat.clear_threads_and_drain()

    assert "cannot answer right now" in parts.chat.chat("hello", thread_id=spec.thread_id)
    assert [thread.thread_id for thread in parts.chat.threads()] == [spec.thread_id]


def test_api_creates_threads_and_routes_threaded_chat(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    parts.chat.set_thread_factory(_factory([], "thread-agent"))

    created = parts.api.execute(ChatThreadCreateQuery(timestamp=REQUEST_TIME, harness=OPUS))
    assert created.chat_thread is not None
    assert created.chat_thread.provider == "claude"
    assert any(event.type is EventType.CHAT_THREAD_CREATED for event in created.events)

    response = parts.api.execute(
        ChatQuery(
            timestamp=REQUEST_TIME, text="what changed?", thread_id=created.chat_thread.thread_id
        )
    )
    assert response.chat is not None
    assert response.chat.answer == "thread-agent: what changed?"
    assert response.chat.thread_id == created.chat_thread.thread_id
    assert [event.chat_thread_id for event in response.events if event.type is EventType.CHAT] == [
        created.chat_thread.thread_id
    ]


def test_api_passes_optional_thread_choices_to_factory(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    calls: list[tuple[str, HarnessChoice | None]] = []
    parts.chat.set_thread_factory(_factory(calls, "thread-agent"))

    created = parts.api.execute(ChatThreadCreateQuery(timestamp=REQUEST_TIME, harness=OPUS))
    assert created.chat_thread is not None
    assert calls == [(created.chat_thread.thread_id, OPUS)]


def test_chat_thread_wire_shapes_round_trip() -> None:
    request = ChatThreadCreateQuery(
        timestamp=REQUEST_TIME, harness=HarnessChoice(provider="codex", model="o4"), title="t"
    )
    assert ChatThreadCreateQuery.model_validate_json(request.model_dump_json()) == request
    query = ChatQuery(timestamp=REQUEST_TIME, text="why?", thread_id="thread-1")
    assert ChatQuery.model_validate_json(query.model_dump_json()) == query
    assert (
        ChatQuery.model_validate(
            {"type": "query.chat", "timestamp": REQUEST_TIME.isoformat(), "text": "why?"}
        ).thread_id
        is None
    )

    event = make_event(
        EventType.CHAT_THREAD_CREATED,
        chat_thread_id="thread-1",
        agent_kind="chat",
        data=ChatThreadCreatedData(
            thread_id="thread-1",
            provider="claude",
            model="opus",
            created_at=_TIMESTAMP,
        ),
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )
    restored = RunEvent.model_validate_json(event.model_dump_json())
    assert restored.chat_thread_id == "thread-1"
    assert isinstance(restored.data, ChatThreadCreatedData)
    assert restored.data.provider == "claude"

    chat_event = make_event(
        EventType.CHAT,
        "why?",
        chat_thread_id="thread-1",
        data=ChatData(answer="because", thread_title="why?"),
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )
    restored_chat = RunEvent.model_validate_json(chat_event.model_dump_json())
    assert isinstance(restored_chat.data, ChatData)
    assert restored_chat.data.thread_title == "why?"

    response = Response.model_validate_json(
        Response(
            timestamp=REQUEST_TIME,
            request_id="r1",
            chat_thread=ChatThreadInfo(
                thread_id="thread-1",
                provider="claude",
                model="opus",
            ),
        ).model_dump_json()
    )
    assert response.chat_thread is not None
    assert response.chat_thread.thread_id == "thread-1"


@pytest.mark.parametrize("thread_id", ["../outside", "con"])
def test_chat_query_rejects_a_nonportable_thread_identity(thread_id: str) -> None:
    with pytest.raises(ValueError, match="invalid experiment chat thread identity") as error:
        ChatQuery(timestamp=REQUEST_TIME, text="why?", thread_id=thread_id)

    assert repr(thread_id) in str(error.value)


def test_a_run_opens_on_the_thread_last_created_then_the_one_last_asked_in(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    parts.chat.install_default_handler(
        lambda question: ChatAnswer(text=f"default: {question}", invocation_id="exec-default")
    )
    parts.chat.set_thread_factory(_factory([], "agent"))
    assert parts.api.snapshot().last_chat_thread_id is None

    first = parts.chat.create_thread(harness=OPUS)
    second = parts.chat.create_thread(harness=OPUS)
    assert parts.api.snapshot().last_chat_thread_id == second.thread_id

    parts.chat.chat("back to the first", thread_id=first.thread_id)
    assert parts.api.snapshot().last_chat_thread_id == first.thread_id

    parts.chat.chat("and the default one")
    assert parts.api.snapshot().last_chat_thread_id is None


def test_the_last_thread_survives_a_restart(tmp_path: Path) -> None:
    first = build_server_parts(tmp_path)
    first.chat.set_thread_factory(_factory([], "agent"))
    older = first.chat.create_thread(harness=OPUS)
    newer = first.chat.create_thread(harness=OPUS)
    first.chat.chat("ask the older thread last", thread_id=older.thread_id)
    assert newer.thread_id != older.thread_id

    resumed = build_server_parts(tmp_path)

    assert resumed.api.snapshot().last_chat_thread_id == older.thread_id
