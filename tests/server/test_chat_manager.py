"""Per-thread experiment chat routing, replay, and wire contracts."""

import json
from pathlib import Path

import pytest
from tests.server.support import build_server_parts

from server.chat.manager import ChatAnswer, ChatThreadFactory, ChatThreadHandle
from server.chat.options import ChatRunSettings
from server.wire import codec, messages
from server.wire.v2 import events_pb2, requests_pb2, responses_pb2, snapshot_pb2


def _factory(
    calls: list[tuple[str, str | None, str | None, str | None]], answer: str
) -> ChatThreadFactory:
    def factory(
        thread_id: str, driver: str | None, provider: str | None, model: str | None
    ) -> ChatThreadHandle:
        calls.append((thread_id, driver, provider, model))
        return ChatThreadHandle(
            spec=events_pb2.ChatThreadCreatedData(
                thread_id=thread_id,
                driver=driver or "agentshim",
                provider=provider or "codex",
                model=model or "gpt-default",
                created_at=messages.now(),
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


def test_created_thread_routes_chat_and_stamps_events(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    parts.chat.install_default_handler(
        lambda question: ChatAnswer(text=f"default: {question}", invocation_id="exec-default")
    )
    calls: list[tuple[str, str | None, str | None, str | None]] = []
    parts.chat.set_thread_factory(_factory(calls, "omnigent-claude"))

    spec = parts.chat.create_thread(driver="omnigent", provider="claude", model="opus")
    assert calls == [(spec.thread_id, "omnigent", "claude", "opus")]
    assert parts.chat.chat("what changed?", thread_id=spec.thread_id) == (
        "omnigent-claude: what changed?"
    )
    assert parts.chat.chat("what changed?") == "default: what changed?"

    created = [
        event for event in _events(tmp_path) if event["type"] == "EVENT_TYPE_CHAT_THREAD_CREATED"
    ]
    assert len(created) == 1
    assert created[0]["chat_thread_id"] == spec.thread_id
    assert created[0]["chat_thread_created"]["driver"] == "omnigent"
    assert created[0]["chat_thread_created"]["provider"] == "claude"
    assert created[0]["chat_thread_created"]["model"] == "opus"
    chats = [event for event in _events(tmp_path) if event["type"] == "EVENT_TYPE_CHAT"]
    assert [event.get("chat_thread_id") for event in chats] == [spec.thread_id, None]
    assert chats[0]["agent_kind"] == "chat"


def test_chat_records_carry_the_answering_invocation_id(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    parts.chat.install_default_handler(
        lambda question: ChatAnswer(text=f"default: {question}", invocation_id="exec-default")
    )
    parts.chat.set_thread_factory(_factory([], "threaded"))
    spec = parts.chat.create_thread()

    parts.chat.chat("what changed?")
    parts.chat.chat("and here?", thread_id=spec.thread_id)

    chats = [event for event in _events(tmp_path) if event["type"] == "EVENT_TYPE_CHAT"]
    # Both routes stamp the terminal record with the invocation that produced
    # the answer, so clients fold it over exactly that streamed turn.
    assert [event["chat"]["invocation_id"] for event in chats] == [
        "exec-default",
        f"exec-{spec.thread_id}",
    ]


def test_fallback_answers_take_fresh_invocation_identities(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)

    parts.chat.chat("what happened?")
    parts.chat.chat("still there?")

    chats = [event for event in _events(tmp_path) if event["type"] == "EVENT_TYPE_CHAT"]
    ids = [event["chat"]["invocation_id"] for event in chats]
    # The read-only fallback never ran an agent, so each answer takes a fresh
    # identity: it must not claim a streamed turn some earlier invocation
    # opened and abandoned.
    assert all(ids)
    assert len(set(ids)) == len(ids) == 2


def test_unknown_thread_returns_clear_answer_without_event(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    answer = parts.chat.chat("hello?", thread_id="missing-thread")
    assert "Unknown experiment chat thread 'missing-thread'" in answer
    assert all(event["type"] != "EVENT_TYPE_CHAT" for event in _events(tmp_path))


def test_thread_creation_without_factory_is_rejected(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    with pytest.raises(RuntimeError, match="chat threads are not available"):
        parts.chat.create_thread()


def test_factory_validation_error_propagates_without_event(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)

    def rejecting_factory(*_args: object) -> ChatThreadHandle:
        raise ValueError("agent driver 'omnigent' does not support provider 'gemini'")  # noqa: TRY003

    parts.chat.set_thread_factory(rejecting_factory)
    with pytest.raises(ValueError, match="does not support provider 'gemini'"):
        parts.chat.create_thread(driver="omnigent", provider="gemini")
    assert all(event["type"] != "EVENT_TYPE_CHAT_THREAD_CREATED" for event in _events(tmp_path))


def test_threads_replay_and_rebuild_on_demand(tmp_path):  # noqa: ANN001, ANN201
    first = build_server_parts(tmp_path)
    first.chat.set_thread_factory(_factory([], "first"))
    spec = first.chat.create_thread(driver="agentshim", provider="claude")
    assert first.chat.chat("why did round two regress so much?", thread_id=spec.thread_id)

    resumed = build_server_parts(tmp_path)
    replayed = resumed.chat.threads()
    assert [thread.thread_id for thread in replayed] == [spec.thread_id]
    assert replayed[0].title == "why did round two regress so much?"
    assert "cannot answer right now" in resumed.chat.chat("still there?", thread_id=spec.thread_id)

    calls: list[tuple[str, str | None, str | None, str | None]] = []
    resumed.chat.set_thread_factory(_factory(calls, "rebuilt"))
    assert resumed.chat.chat("still there?", thread_id=spec.thread_id) == ("rebuilt: still there?")
    assert calls == [(spec.thread_id, "agentshim", "claude", "gpt-default")]


def test_first_message_titles_an_untitled_thread_once(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    parts.chat.set_thread_factory(_factory([], "answer"))
    spec = parts.chat.create_thread()

    parts.chat.chat("explain why the benchmark throughput regressed in round four", spec.thread_id)
    parts.chat.chat("and round five?", thread_id=spec.thread_id)

    chats = [event for event in _events(tmp_path) if event["type"] == "EVENT_TYPE_CHAT"]
    assert chats[0]["chat"]["thread_title"] == "explain why the benchmark throughput…"
    assert "thread_title" not in chats[1]["chat"]
    assert parts.chat.threads()[0].title == "explain why the benchmark throughput…"


def test_explicit_title_is_authoritative(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    parts.chat.set_thread_factory(_factory([], "answer"))
    spec = parts.chat.create_thread(title="  perf deep dive  ")
    parts.chat.chat("first question", thread_id=spec.thread_id)

    assert spec.title == "perf deep dive"
    chats = [event for event in _events(tmp_path) if event["type"] == "EVENT_TYPE_CHAT"]
    assert "thread_title" not in chats[0]["chat"]
    assert parts.chat.threads()[0].title == "perf deep dive"


def test_cleared_threads_stop_routing_but_keep_replayable_specs(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    parts.chat.set_thread_factory(_factory([], "answer"))
    spec = parts.chat.create_thread()
    parts.chat.clear_threads_and_drain()

    assert "cannot answer right now" in parts.chat.chat("hello", thread_id=spec.thread_id)
    assert [thread.thread_id for thread in parts.chat.threads()] == [spec.thread_id]


def test_api_creates_threads_and_routes_threaded_chat(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    parts.chat.set_thread_factory(_factory([], "thread-agent"))

    created = parts.api.execute(messages.make_request("chat_thread_create", provider="claude"))
    assert created.HasField("chat_thread")
    assert created.chat_thread.provider == "claude"
    assert created.chat_thread.driver == "agentshim"
    assert any(event.type == events_pb2.EVENT_TYPE_CHAT_THREAD_CREATED for event in created.events)

    response = parts.api.execute(
        messages.make_request("chat", text="what changed?", thread_id=created.chat_thread.thread_id)
    )
    assert response.HasField("chat")
    assert response.chat.answer == "thread-agent: what changed?"
    assert response.chat.thread_id == created.chat_thread.thread_id
    assert [
        event.chat_thread_id
        for event in response.events
        if event.type == events_pb2.EVENT_TYPE_CHAT
    ] == [created.chat_thread.thread_id]


def test_chat_options_group_by_provider_and_mark_run_model(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    parts.chat.set_run_settings(
        ChatRunSettings(
            driver="omnigent",
            provider="codex",
            model="gpt-5.5-run",
            role_models=("gpt-5.6-outer", "gpt-5.5-run"),
        )
    )
    options = parts.api.execute(messages.make_request("chat_options")).chat_options
    assert [group.provider for group in options.providers] == ["claude", "codex"]
    assert "driver" not in codec.to_dict(options)

    codex = next(group for group in options.providers if group.provider == "codex")
    assert [option.model for option in codex.models[:2]] == [
        "gpt-5.5-run",
        "gpt-5.6-outer",
    ]
    assert codex.models[0].default is True
    assert codex.models[1].source == responses_pb2.CHAT_MODEL_SOURCE_ROLE
    assert {option.source for option in codex.models[2:]} == {
        responses_pb2.CHAT_MODEL_SOURCE_SUGGESTED
    }
    claude = next(group for group in options.providers if group.provider == "claude")
    assert not any(option.default for option in claude.models)


def test_chat_options_are_absent_before_run_settings_attach(tmp_path):  # noqa: ANN001, ANN201
    response = build_server_parts(tmp_path).api.execute(messages.make_request("chat_options"))
    assert response.ok is True
    assert not response.HasField("chat_options")


def test_api_passes_optional_thread_choices_to_factory(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    calls: list[tuple[str, str | None, str | None, str | None]] = []
    parts.chat.set_thread_factory(_factory(calls, "thread-agent"))

    created = parts.api.execute(
        messages.make_request("chat_thread_create", provider="claude", model="opus")
    )
    assert created.HasField("chat_thread")
    assert calls == [(created.chat_thread.thread_id, None, "claude", "opus")]
    assert created.chat_thread.driver == "agentshim"


def test_chat_thread_wire_shapes_round_trip() -> None:
    request = messages.make_request(
        "chat_thread_create", driver=requests_pb2.CHAT_DRIVER_OMNIGENT, provider="codex", title="t"
    )
    assert codec.parse_request(codec.dumps(request)) == request
    query = messages.make_request("chat", text="why?", thread_id="thread-1")
    assert codec.parse_request(codec.dumps(query)) == query
    assert not messages.make_request("chat", text="why?").chat.HasField("thread_id")

    event = messages.make_event(
        events_pb2.EVENT_TYPE_CHAT_THREAD_CREATED,
        chat_thread_id="thread-1",
        agent_kind="chat",
        data=events_pb2.ChatThreadCreatedData(
            thread_id="thread-1",
            driver="agentshim",
            provider="claude",
            model="opus",
            created_at=messages.now(),
        ),
    )
    restored = codec.loads(events_pb2.RunEvent, codec.dumps(event))
    assert restored.chat_thread_id == "thread-1"
    payload = messages.payload_of(restored)
    assert isinstance(payload, events_pb2.ChatThreadCreatedData)
    assert payload.provider == "claude"

    chat_event = messages.make_event(
        events_pb2.EVENT_TYPE_CHAT,
        "why?",
        chat_thread_id="thread-1",
        data=events_pb2.ChatData(answer="because", thread_title="why?"),
    )
    restored_chat = codec.loads(events_pb2.RunEvent, codec.dumps(chat_event))
    chat_payload = messages.payload_of(restored_chat)
    assert isinstance(chat_payload, events_pb2.ChatData)
    assert chat_payload.thread_title == "why?"

    response = codec.loads(
        responses_pb2.Response,
        codec.dumps(
            responses_pb2.Response(
                request_id="r1",
                chat_thread=snapshot_pb2.ChatThreadInfo(
                    thread_id="thread-1",
                    driver="agentshim",
                    provider="claude",
                    model="opus",
                ),
            )
        ),
    )
    assert response.HasField("chat_thread")
    assert response.chat_thread.thread_id == "thread-1"
