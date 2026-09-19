"""Request API and Unix JSONL transport boundary tests."""

import json
import socket
import uuid
from pathlib import Path

import pytest
from tests.server.support import build_server_parts

from server.chat.manager import ChatAnswer
from server.run_lifecycle import RunStatus
from server.transport.unix_jsonl import UnixJsonlServer
from server.wire import codec, messages
from server.wire.v2 import events_pb2
from vibesys.unix_socket import (
    MAX_SOCKET_PATH_BYTES,
    SocketPathTooLongError,
    validate_socket_path,
)


def _request(socket_path: Path, request) -> dict:  # noqa: ANN001
    """Send a typed request, or a raw JSON object for malformed-input cases."""
    line = codec.dumps(request) if not isinstance(request, dict) else json.dumps(request)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        stream = client.makefile("rwb")
        stream.write(line.encode() + b"\n")
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

    response = parts.api.execute(messages.make_request("chat", text="what changed?"))

    assert response.HasField("chat")
    assert response.chat.answer == "agent answer"
    assert questions == ["what changed?"]
    assert response.events[-1].type == events_pb2.EVENT_TYPE_CHAT
    assert response.events[-1].agent_kind == "chat"


def test_api_fallback_explains_agent_availability(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    answer = parts.chat.chat("what happened in this experiment?")
    assert "chat agent is not available" in answer
    assert "not finished starting up" in answer
    assert "/history" not in answer

    parts.controller.finish()
    assert "the run has finished" in parts.chat.chat("what happened?")


def test_transport_round_trips_the_stop_command(tmp_path):  # noqa: ANN001, ANN201
    """`stop` parses off the wire, dispatches, and acks as pending."""
    parts = build_server_parts(tmp_path / "logs")
    socket_path = Path("/tmp") / f"vibesys-test-{uuid.uuid4().hex}.sock"  # noqa: S108

    with UnixJsonlServer(socket_path, parts.api):
        response = _request(socket_path, messages.make_request("stop"))

    assert response["ok"] is True
    assert response["ack"] == {
        "action": "COMMAND_ACTION_STOP",
        "status": "COMMAND_ACK_STATUS_PENDING",
    }
    assert parts.controller.run_status() is RunStatus.STOPPING


def test_transport_supports_multiple_clients_and_replay(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path / "logs")
    socket_path = Path("/tmp") / f"vibesys-test-{uuid.uuid4().hex}.sock"  # noqa: S108

    with UnixJsonlServer(socket_path, parts.api):
        status = _request(socket_path, messages.make_request("snapshot"))
        replay = _request(socket_path, messages.make_request("events", after_sequence=0))

    assert status["ok"] is True
    assert status["snapshot"]["status"] == "RUN_STATUS_RUNNING"
    sequences = [event["sequence"] for event in replay["events"]]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))
    assert any(event["type"] == "EVENT_TYPE_SERVER_STARTED" for event in replay["events"])


def test_transport_returns_sanitized_request_errors(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path / "logs")

    def fail_chat(question: str) -> ChatAnswer:
        raise RuntimeError(  # noqa: TRY003
            f"token=super-secret Chat agent failed while answering: {question}"
        )

    parts.chat.install_default_handler(fail_chat)
    socket_path = Path("/tmp") / f"vibesys-test-{uuid.uuid4().hex}.sock"  # noqa: S108

    with UnixJsonlServer(socket_path, parts.api):
        response = _request(socket_path, messages.make_request("chat", text="what happened?"))

    assert response.get("ok", False) is False  # proto3 JSON omits a false bool
    assert response["error"] == "Request failed"
    assert response["diagnostic"]["scope"] == "DIAGNOSTIC_SCOPE_REQUEST"
    assert response["diagnostic"]["detail"] == (
        "RuntimeError: token=[REDACTED] Chat agent failed while answering: what happened?"
    )


def test_transport_rejects_unknown_fields_and_keeps_the_connection(tmp_path):  # noqa: ANN001, ANN201
    """An unknown field is a capability probe: `invalid_message`, connection stays open."""
    parts = build_server_parts(tmp_path / "logs")
    socket_path = Path("/tmp") / f"vibesys-test-{uuid.uuid4().hex}.sock"  # noqa: S108
    probe = {
        "protocol_version": 2,
        "request_id": "probe-1",
        "subscribe": {"after_sequence": 0, "field_from_the_future": 1},
    }

    with UnixJsonlServer(socket_path, parts.api):  # noqa: SIM117
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(socket_path))
            stream = client.makefile("rwb")
            stream.write(json.dumps(probe).encode() + b"\n")
            stream.write(codec.dumps(messages.make_request("snapshot")).encode() + b"\n")
            stream.flush()
            rejected = json.loads(stream.readline())["protocol_error"]
            answered = json.loads(stream.readline())

    assert rejected["code"] == "invalid_message"
    assert rejected["request_id"] == "probe-1"
    assert "field_from_the_future" in rejected["message"]
    assert answered["ok"] is True


def test_transport_rejects_version_one_clients_with_a_clear_message(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path / "logs")
    socket_path = Path("/tmp") / f"vibesys-test-{uuid.uuid4().hex}.sock"  # noqa: S108

    with UnixJsonlServer(socket_path, parts.api):
        reply = _request(socket_path, {"type": "query.snapshot", "request_id": "old-1"})

    error = reply["protocol_error"]
    assert error["code"] == "protocol_version_unsupported"
    assert error["request_id"] == "old-1"
    assert "Upgrade the client" in error["message"]


@pytest.mark.parametrize(
    ("line", "code"),
    [
        (b"not json\n", "invalid_json"),
        (b"[]\n", "invalid_message"),
        (b'{"protocol_version":2}\n', "invalid_message"),
        (b'{"protocol_version":2,"steer":{"text":""}}\n', "invalid_message"),
        (b'{"protocol_version":2,"events":{"timeout_ms":30001}}\n', "invalid_message"),
    ],
)
def test_transport_reports_malformed_requests_by_code(tmp_path, line, code):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path / "logs")
    socket_path = Path("/tmp") / f"vibesys-test-{uuid.uuid4().hex}.sock"  # noqa: S108

    with UnixJsonlServer(socket_path, parts.api):  # noqa: SIM117
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(socket_path))
            stream = client.makefile("rwb")
            stream.write(line)
            stream.flush()
            reply = json.loads(stream.readline())

    assert reply["protocol_error"]["code"] == code


def test_subscription_streams_one_consistent_append_batch(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path / "logs")
    socket_path = Path("/tmp") / f"vibesys-test-{uuid.uuid4().hex}.sock"  # noqa: S108

    with UnixJsonlServer(socket_path, parts.api):  # noqa: SIM117
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(socket_path))
            stream = client.makefile("rwb")
            stream.write(codec.dumps(messages.make_request("subscribe")).encode() + b"\n")
            stream.flush()
            subscribed = json.loads(stream.readline())
            replay = json.loads(stream.readline())
            with parts.condition:
                parts.journal.record(
                    events_pb2.EVENT_TYPE_CHAT, "hello", status=events_pb2.EVENT_STATUS_ANSWERED
                )
                parts.journal.record(events_pb2.EVENT_TYPE_STATUS_QUERY, "/history")
            streamed = json.loads(stream.readline())

    assert "subscribed" in subscribed
    assert "event_batch" in replay
    batch = streamed["event_batch"]
    assert [event["type"] for event in batch["events"]] == [
        "EVENT_TYPE_CHAT",
        "EVENT_TYPE_STATUS_QUERY",
    ]
    assert batch["through_sequence"] == batch["events"][-1]["sequence"]


def test_subscription_reports_structured_stream_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parts = build_server_parts(tmp_path / "logs")
    socket_path = Path("/tmp") / f"vibesys-test-{uuid.uuid4().hex}.sock"  # noqa: S108

    def fail_replay(  # noqa: ANN202
        after_sequence: int, *, store_id: str | None = None, bootstrap_spine: bool = False
    ):
        del after_sequence, store_id, bootstrap_spine
        raise RuntimeError("event store is unavailable")  # noqa: TRY003

    monkeypatch.setattr(parts.api, "subscription_checkpoint", fail_replay)
    with UnixJsonlServer(socket_path, parts.api):  # noqa: SIM117
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(socket_path))
            stream = client.makefile("rwb")
            stream.write(codec.dumps(messages.make_request("subscribe")).encode() + b"\n")
            stream.flush()
            subscribed = json.loads(stream.readline())
            bootstrap = json.loads(stream.readline())
            parts.journal.record(
                events_pb2.EVENT_TYPE_CHAT, "hello", status=events_pb2.EVENT_STATUS_ANSWERED
            )
            failure = json.loads(stream.readline())

    assert "subscribed" in subscribed
    assert "event_batch" in bootstrap
    error = failure["protocol_error"]
    assert error["request_id"] == subscribed["subscribed"]["request_id"]
    assert error["code"] == "stream_failed"
    assert error["diagnostic"]["detail"] == "RuntimeError: event store is unavailable"


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
