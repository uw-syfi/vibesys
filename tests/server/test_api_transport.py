"""Request API and Unix JSONL transport boundary tests."""

import json
import socket
import uuid
from pathlib import Path

import pytest
from tests.server.support import build_server_parts

from server.api.protocol import (
    ChatQuery,
    EventsQuery,
    SnapshotQuery,
    SubscribeRequest,
)
from server.chat.manager import ChatAnswer
from server.events import EventType
from server.transport.unix_jsonl import UnixJsonlServer
from vibesys.unix_socket import (
    MAX_SOCKET_PATH_BYTES,
    SocketPathTooLongError,
    validate_socket_path,
)


def _request(socket_path: Path, request) -> dict:  # noqa: ANN001
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        stream = client.makefile("rwb")
        stream.write(request.model_dump_json().encode() + b"\n")
        stream.flush()
        return json.loads(stream.readline())


def test_api_routes_chat_to_configured_handler(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    questions: list[str] = []
    parts.chat.install_default_handler(
        lambda question: (
            questions.append(question) or ChatAnswer(text="agent answer", invocation_id="exec-1")
        )
    )

    response = parts.api.execute(ChatQuery(text="what changed?"))

    assert response.chat is not None
    assert response.chat.answer == "agent answer"
    assert questions == ["what changed?"]
    assert response.events[-1].type is EventType.CHAT
    assert response.events[-1].agent_kind == "chat"


def test_api_fallback_explains_agent_availability(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    answer = parts.chat.chat("what happened in this experiment?")
    assert "chat agent is not available" in answer
    assert "not finished starting up" in answer
    assert "/history" not in answer

    parts.controller.finish()
    assert "the run has finished" in parts.chat.chat("what happened?")


def test_transport_supports_multiple_clients_and_replay(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path / "logs")
    socket_path = Path("/tmp") / f"vibesys-test-{uuid.uuid4().hex}.sock"  # noqa: S108

    with UnixJsonlServer(socket_path, parts.api):
        status = _request(socket_path, SnapshotQuery())
        replay = _request(socket_path, EventsQuery(after_sequence=0))

    assert status["ok"] is True
    assert status["snapshot"]["status"] == "running"
    sequences = [event["sequence"] for event in replay["events"]]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))
    assert any(event["type"] == "server_started" for event in replay["events"])


def test_transport_returns_sanitized_request_errors(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path / "logs")

    def fail_chat(question: str) -> ChatAnswer:
        raise RuntimeError(  # noqa: TRY003
            f"token=super-secret Chat agent failed while answering: {question}"
        )

    parts.chat.install_default_handler(fail_chat)
    socket_path = Path("/tmp") / f"vibesys-test-{uuid.uuid4().hex}.sock"  # noqa: S108

    with UnixJsonlServer(socket_path, parts.api):
        response = _request(socket_path, ChatQuery(text="what happened?"))

    assert response["ok"] is False
    assert response["error"] == "Request failed"
    assert response["diagnostic"]["scope"] == "request"
    assert response["diagnostic"]["detail"] == (
        "RuntimeError: token=[REDACTED] Chat agent failed while answering: what happened?"
    )


def test_subscription_streams_one_consistent_append_batch(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path / "logs")
    socket_path = Path("/tmp") / f"vibesys-test-{uuid.uuid4().hex}.sock"  # noqa: S108

    with UnixJsonlServer(socket_path, parts.api):  # noqa: SIM117
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(socket_path))
            stream = client.makefile("rwb")
            stream.write(SubscribeRequest(after_sequence=0).model_dump_json().encode() + b"\n")
            stream.flush()
            subscribed = json.loads(stream.readline())
            replay = json.loads(stream.readline())
            with parts.condition:
                parts.journal.record(EventType.CHAT, "hello", status="answered")
                parts.journal.record(EventType.STATUS_QUERY, "/history")
            streamed = json.loads(stream.readline())

    assert subscribed["type"] == "subscribed"
    assert replay["type"] == "event_batch"
    assert streamed["type"] == "event_batch"
    assert [event["type"] for event in streamed["events"]] == ["chat", "status_query"]
    assert streamed["through_sequence"] == streamed["events"][-1]["sequence"]


def test_subscription_reports_structured_stream_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parts = build_server_parts(tmp_path / "logs")
    socket_path = Path("/tmp") / f"vibesys-test-{uuid.uuid4().hex}.sock"  # noqa: S108

    def fail_replay(after_sequence: int, *, bootstrap_spine: bool = False):  # noqa: ANN202
        del after_sequence, bootstrap_spine
        raise RuntimeError("event store is unavailable")  # noqa: TRY003

    monkeypatch.setattr(parts.api, "subscription_checkpoint", fail_replay)
    with UnixJsonlServer(socket_path, parts.api):  # noqa: SIM117
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(socket_path))
            stream = client.makefile("rwb")
            stream.write(SubscribeRequest(after_sequence=0).model_dump_json().encode() + b"\n")
            stream.flush()
            subscribed = json.loads(stream.readline())
            bootstrap = json.loads(stream.readline())
            parts.journal.record(EventType.CHAT, "hello", status="answered")
            failure = json.loads(stream.readline())

    assert subscribed["type"] == "subscribed"
    assert bootstrap["type"] == "event_batch"
    assert failure["type"] == "protocol_error"
    assert failure["request_id"] == subscribed["request_id"]
    assert failure["code"] == "stream_failed"
    assert failure["diagnostic"]["detail"] == "RuntimeError: event store is unavailable"


def test_socket_path_limit_matches_kernel(socket_dir: Path) -> None:
    name = "a" * (MAX_SOCKET_PATH_BYTES - len(str(socket_dir)) - 1)
    longest = socket_dir / name

    assert validate_socket_path(longest) is longest
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as accepted:
        accepted.bind(str(longest))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as rejected:  # noqa: SIM117
        with pytest.raises(OSError, match="too long"):
            rejected.bind(f"{longest}a")


def test_transport_rejects_overlong_path_before_binding(tmp_path: Path) -> None:
    path = tmp_path / ("d" * MAX_SOCKET_PATH_BYTES) / "server.sock"
    parts = build_server_parts()

    with pytest.raises(SocketPathTooLongError) as failure:
        UnixJsonlServer(path, parts.api).start()

    assert failure.value.path == path
    assert str(MAX_SOCKET_PATH_BYTES) in str(failure.value)
    assert not path.parent.exists()
