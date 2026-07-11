import json
import socket
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import Mock

from vibe_serve.context import _RunContext
from vibe_serve.server import (
    EventType,
    RunInspector,
    RunSupervisor,
)
from vibe_serve.server.protocol import ChatQuery, EventsQuery, SnapshotQuery, SubscribeRequest
from vibe_serve.server.schema import ProtocolDocument
from vibe_serve.server.service import SupervisionService
from vibe_serve.server.transport import SupervisionSocketServer


def _events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_chat_is_audited_but_not_injected(tmp_path):
    supervisor = RunSupervisor()
    supervisor.attach(tmp_path)
    supervisor.record(EventType.CHAT, "What is happening?", status="answered")
    supervisor.before_agent("judge", "round 2", "original prompt")
    started = next(
        event for event in supervisor.read_events() if event.type == "invocation_started"
    )
    assert started.data.user_prompt == "original prompt"
    event_types = [event["type"] for event in _events(tmp_path / "run-events.jsonl")]
    assert event_types.index("chat") < event_types.index("invocation_started")


def test_pause_takes_effect_at_next_safe_point(tmp_path):
    supervisor = RunSupervisor()
    supervisor.attach(tmp_path)
    supervisor.pause_after_call()
    supervisor.after_agent("implementer", "round 1")
    result = []
    waiter = threading.Thread(
        target=lambda: result.append(supervisor.before_agent("judge", "round 1", "prompt"))
    )
    waiter.start()
    time.sleep(0.02)
    assert waiter.is_alive()
    supervisor.resume()
    waiter.join(timeout=1)
    assert result == [None]


def test_invocation_audit_contains_prompts_and_result(tmp_path):
    supervisor = RunSupervisor()
    supervisor.attach(tmp_path)
    supervisor.before_agent("implementer", "round 4", "Do work", "System rules")
    supervisor.after_agent("implementer", "round 4", result={"summary": "done"})

    events = _events(tmp_path / "run-events.jsonl")
    started = next(e for e in events if e["type"] == "invocation_started")
    finished = next(e for e in events if e["type"] == "invocation_finished")
    assert started["data"]["system_prompt"] == "System rules"
    assert started["data"]["user_prompt"] == "Do work"
    assert finished["invocation_id"] == started["invocation_id"]
    assert finished["data"]["result"] == {"summary": "done"}


def test_inspector_answers_round_and_failure_queries(tmp_path):
    supervisor = RunSupervisor()
    (tmp_path / "logs").mkdir()
    supervisor.attach(tmp_path / "logs")
    (supervisor.log_dir / "progress.md").write_text(
        "## Round 1 — Judge\nPASS\n\n## Round 2 — Judge\nFAIL: latency regressed\n"
    )
    inspector = RunInspector(supervisor)
    assert "Round 2" in inspector.round_detail(2)
    assert "latency regressed" in inspector.answer("why did the judge fail?")


def test_general_chat_is_distinct_from_status_query(tmp_path):
    supervisor = RunSupervisor()
    supervisor.attach(tmp_path)
    supervisor.chat("hello there")
    event_types = [event["type"] for event in _events(tmp_path / "run-events.jsonl")]
    assert event_types == ["server_started", "chat"]


def test_chat_reports_structured_failed_invocation(tmp_path):
    supervisor = RunSupervisor()
    supervisor.attach(tmp_path)
    supervisor.before_agent("implementer", "round 5", "prompt")
    supervisor.after_agent("implementer", "round 5", error=RuntimeError("agent process exited"))
    answer = supervisor.chat("why did the agent fail?")
    assert "Latest failed agent invocation" in answer
    assert "agent process exited" in answer


def test_service_accepts_chat(tmp_path):
    (tmp_path / "logs").mkdir()
    supervisor = RunSupervisor()
    supervisor.attach(tmp_path / "logs")
    service = SupervisionService(supervisor)
    chat = service.execute(ChatQuery(text="what is the current status?"))
    assert chat.chat.question == "what is the current status?"
    events = _events(tmp_path / "logs" / "run-events.jsonl")
    assert any(event["type"] == "chat" for event in events)
    assert any(event["type"] == "status_query" for event in events)


def test_socket_transport_supports_multiple_clients_and_event_replay(tmp_path):
    supervisor = RunSupervisor()
    supervisor.attach(tmp_path / "logs")
    service = SupervisionService(supervisor)
    socket_path = Path("/tmp") / f"vibeserve-test-{uuid.uuid4().hex}.sock"

    with SupervisionSocketServer(socket_path, service):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as first:
            first.connect(str(socket_path))
            first_file = first.makefile("rwb")
            first_file.write(SnapshotQuery().model_dump_json().encode() + b"\n")
            first_file.flush()
            status = json.loads(first_file.readline())
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as second:
            second.connect(str(socket_path))
            second_file = second.makefile("rwb")
            second_file.write(EventsQuery(after_sequence=0).model_dump_json().encode() + b"\n")
            second_file.flush()
            replay = json.loads(second_file.readline())

    assert status["ok"] is True
    assert status["snapshot"]["status"] == "running"
    sequences = [event["sequence"] for event in replay["events"]]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))
    assert any(event["type"] == "server_started" for event in replay["events"])


def test_socket_subscription_replays_then_streams_new_events(tmp_path):
    supervisor = RunSupervisor()
    supervisor.attach(tmp_path / "logs")
    service = SupervisionService(supervisor)
    socket_path = Path("/tmp") / f"vibeserve-test-{uuid.uuid4().hex}.sock"

    with SupervisionSocketServer(socket_path, service):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(socket_path))
            stream = client.makefile("rwb")
            request = SubscribeRequest(after_sequence=0)
            stream.write(request.model_dump_json().encode() + b"\n")
            stream.flush()
            subscribed = json.loads(stream.readline())
            replay = json.loads(stream.readline())
            supervisor.record(EventType.CHAT, "hello", status="answered")
            streamed = json.loads(stream.readline())

    assert subscribed["type"] == "subscribed"
    assert replay["type"] == "event_batch"
    assert streamed["type"] == "event"
    assert streamed["event"]["type"] == "chat"


def test_run_context_records_invocation_boundary(tmp_path):
    supervisor = RunSupervisor()
    supervisor.attach(tmp_path)
    ctx = _RunContext.__new__(_RunContext)
    ctx.supervisor = supervisor
    ctx.agent_runner = Mock()
    ctx.agent_runner.invoke.return_value = {"summary": "measured"}
    ctx.workspace = tmp_path
    ctx.gpu_env = lambda: {}
    ctx._progress_stack = []

    result = ctx.invoke(
        kind="implementer",
        system_prompt="system",
        user_prompt="original",
        response_cls=dict,
        round_label="round 6 attempt 2",
    )

    assert result == {"summary": "measured"}
    sent_prompt = ctx.agent_runner.invoke.call_args.kwargs["user_prompt"]
    assert sent_prompt == "original"
    events = _events(tmp_path / "run-events.jsonl")
    started = next(event for event in events if event["type"] == "invocation_started")
    assert started["data"]["user_prompt"] == "original"


def test_committed_protocol_schema_matches_python_contract():
    schema_path = Path("clients/tui/src/protocol.schema.json")
    assert json.loads(schema_path.read_text()) == ProtocolDocument.model_json_schema()
