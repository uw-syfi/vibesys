import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import Mock

import pytest

from vibe_sys.context import _RunContext
from vibe_sys.errors import ConfigurationDiagnostic, ConfigurationError
from vibe_sys.server import (
    EventType,
    RunInspector,
    RunSupervisor,
)
from vibe_sys.server.protocol import (
    ChatQuery,
    EventsQuery,
    HistoryQuery,
    SnapshotQuery,
    SubscribeRequest,
)
from vibe_sys.server.runtime import run_server
from vibe_sys.server.schema import ProtocolDocument
from vibe_sys.server.service import SupervisionService
from vibe_sys.server.transport import SupervisionSocketServer


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


def test_bootstrap_events_migrate_to_run_audit_without_replacing_history(tmp_path):
    logs = tmp_path / "run" / "logs"
    historical = RunSupervisor()
    historical.attach(logs)
    historical.record(EventType.RUN_FINISHED, "previous invocation", status="completed")

    bootstrap = tmp_path / "session"
    supervisor = RunSupervisor()
    supervisor.attach(bootstrap)
    supervisor.record(EventType.SERVER_READY, status="active")
    supervisor.attach(logs)
    supervisor.record(EventType.RUN_STARTED, status="active")

    audited = _events(logs / "run-events.jsonl")
    assert [event["type"] for event in audited] == [
        "server_started",
        "run_finished",
        "server_started",
        "server_ready",
        "run_started",
    ]
    assert [event.type for event in supervisor.read_events()] == [
        "server_started",
        "server_ready",
        "run_started",
    ]
    assert [event.type for event in supervisor.read_history_events()] == [
        "server_started",
        "run_finished",
        "server_started",
        "server_ready",
        "run_started",
    ]


def test_history_query_reads_prior_and_current_session_events(tmp_path):
    logs = tmp_path / "run" / "logs"
    historical = RunSupervisor()
    historical.attach(logs)
    historical.record(EventType.ROUND_FINISHED, status="completed", round_label="round-1")

    supervisor = RunSupervisor()
    supervisor.attach(tmp_path / "session")
    supervisor.attach(logs)
    supervisor.record(EventType.ROUND_FINISHED, status="completed", round_label="round-2")

    response = SupervisionService(supervisor).execute(HistoryQuery())

    assert [event.round_label for event in response.events if event.round_label] == [
        "round-1",
        "round-2",
    ]
    assert {event.round_label for event in supervisor.read_events()} == {None, "round-2"}


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
    socket_path = Path("/tmp") / f"vibesys-test-{uuid.uuid4().hex}.sock"

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
    socket_path = Path("/tmp") / f"vibesys-test-{uuid.uuid4().hex}.sock"

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


def test_cli_parse_failure_is_streamed_after_client_attaches():
    session_dir = Path("/tmp") / f"vs-test-{uuid.uuid4().hex}"
    session_dir.mkdir()
    socket_path = session_dir / "control.sock"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "vibe_sys.cli",
            "--headless",
            "--control-socket",
            str(socket_path),
            "--not-a-real-option",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path.cwd() / "src")},
    )
    try:
        deadline = time.monotonic() + 5
        while not socket_path.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        if not socket_path.exists():
            output, error = process.communicate(timeout=5)
            pytest.fail(
                f"backend did not create control socket: stdout={output!r} stderr={error!r}"
            )

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(str(socket_path))
            stream = client.makefile("rwb")
            stream.write(SubscribeRequest(after_sequence=0).model_dump_json().encode() + b"\n")
            stream.flush()
            messages = []
            while True:
                line = stream.readline()
                if not line:
                    break
                messages.append(json.loads(line))
                events = []
                for message in messages:
                    if message["type"] == "event":
                        events.append(message["event"])
                    elif message["type"] == "event_batch":
                        events.extend(message["events"])
                if any(event["type"] == "configuration_failed" for event in events):
                    break
            stream.close()

        assert process.wait(timeout=5) == 2
        audited_events = _events(session_dir / "run-events.jsonl")
    finally:
        if process.poll() is None:
            process.kill()
        stdout, stderr = process.communicate(timeout=5)
        shutil.rmtree(session_dir, ignore_errors=True)

    failures = [event for event in events if event["type"] == "configuration_failed"]
    assert failures[0]["data"]["code"] == "invalid_arguments"
    assert "--not-a-real-option" in failures[0]["data"]["message"]
    assert [event["type"] for event in audited_events].count("configuration_failed") == 1
    assert not any(event["type"] == "run_failed" for event in audited_events)
    assert stdout == ""
    assert stderr == ""


def test_supervision_runtime_streams_configuration_failure_before_exiting():
    session_dir = Path("/tmp") / f"vs-runtime-test-{uuid.uuid4().hex}"
    socket_path = session_dir / "control.sock"
    received_events = []

    def subscribe_until_failure() -> None:
        deadline = time.monotonic() + 5
        while not socket_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(str(socket_path))
            stream = client.makefile("rwb")
            stream.write(SubscribeRequest(after_sequence=0).model_dump_json().encode() + b"\n")
            stream.flush()
            while True:
                message = json.loads(stream.readline())
                if message["type"] == "event":
                    received_events.append(message["event"])
                elif message["type"] == "event_batch":
                    received_events.extend(message["events"])
                if any(event["type"] == "configuration_failed" for event in received_events):
                    break
            stream.close()

    subscriber = threading.Thread(target=subscribe_until_failure)
    subscriber.start()
    failure = ConfigurationError(
        ConfigurationDiagnostic(
            code="invalid_arguments",
            stage="argument_parsing",
            message="unknown option --bad",
            usage="usage: vibe-sys ...",
        )
    )
    try:
        with pytest.raises(ConfigurationError) as raised:
            run_server(lambda: (_ for _ in ()).throw(failure), socket_path=socket_path)
        assert raised.value is failure
        subscriber.join(timeout=5)
        assert not subscriber.is_alive()
        configuration_event = next(
            event for event in received_events if event["type"] == "configuration_failed"
        )
        assert configuration_event["data"] == {
            "kind": "configuration_failed",
            "code": "invalid_arguments",
            "stage": "argument_parsing",
            "message": "unknown option --bad",
            "usage": "usage: vibe-sys ...",
            "exit_code": 2,
        }
        assert not any(event["type"] == "run_failed" for event in received_events)
    finally:
        subscriber.join(timeout=5)
        shutil.rmtree(session_dir, ignore_errors=True)


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
    schema_path = Path("clients/tui/src/generated/protocol.schema.json")
    assert json.loads(schema_path.read_text()) == ProtocolDocument.model_json_schema()
