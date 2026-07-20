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

from vibesys.context import _RunContext
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.run import RunPaths
from vibesys.server import (
    EventType,
    RunInspector,
    RunSupervisor,
)
from vibesys.server.events import ConfigurationFailedData
from vibesys.server.protocol import (
    ChatQuery,
    EventsQuery,
    HistoryQuery,
    PerformanceQuery,
    SnapshotQuery,
    SubscribeRequest,
)
from vibesys.server.runtime import run_server
from vibesys.server.schema import ProtocolDocument
from vibesys.server.service import SupervisionService
from vibesys.server.transport import SupervisionSocketServer


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


def test_side_channel_chat_output_is_tagged_without_changing_active_agent(tmp_path):
    supervisor = RunSupervisor()
    supervisor.attach(tmp_path)
    supervisor.before_agent("implementer", "round-1", "work")

    with supervisor.presentation_scope(
        agent_kind="chat", round_label="experiment-chat", invocation_id="chat-1"
    ):
        supervisor.publish_agent_output("private chat output")
    supervisor.publish_agent_output("experiment output")

    agent_events = [
        event for event in supervisor.read_events() if event.type is EventType.AGENT_OUTPUT_CHUNK
    ]
    assert [event.agent_kind for event in agent_events] == ["chat", "implementer"]
    assert agent_events[0].round_label == "experiment-chat"
    assert agent_events[0].invocation_id == "chat-1"
    assert agent_events[1].data.content == "experiment output"


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


def test_performance_query_reads_rounds_json(tmp_path):
    logs = tmp_path / "run" / "logs"
    logs.mkdir(parents=True)
    (logs / "rounds.json").write_text(
        json.dumps(
            [
                {
                    "round": 1,
                    "perf_metric": 1200.0,
                    "perf_unit": "total_ops_per_sec",
                    "passed": True,
                    "profile_skipped": False,
                },
                {
                    "round": 2,
                    "perf_metric": None,
                    "perf_unit": None,
                    "passed": False,
                    "profile_skipped": True,
                },
                {
                    "round": 3,
                    "perf_metric": 2400.0,
                    "perf_unit": "total_ops_per_sec",
                    "passed": True,
                    "profile_skipped": False,
                },
            ]
        )
    )
    supervisor = RunSupervisor()
    supervisor.attach(logs)

    response = SupervisionService(supervisor).execute(PerformanceQuery())

    assert [round.round for round in response.performance] == [1, 3]
    assert response.performance[1].perf_metric == 2400.0
    assert response.performance[1].perf_unit == "total_ops_per_sec"


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


def test_service_routes_chat_to_configured_agent_handler(tmp_path):
    supervisor = RunSupervisor()
    supervisor.attach(tmp_path)
    questions = []
    supervisor.set_chat_handler(lambda question: questions.append(question) or "agent answer")

    response = SupervisionService(supervisor).execute(ChatQuery(text="what changed?"))

    assert response.chat.answer == "agent answer"
    assert questions == ["what changed?"]
    assert response.events[-1].type is EventType.CHAT
    assert response.events[-1].agent_kind == "chat"
    assert _events(tmp_path / "run-events.jsonl")[-1]["type"] == "chat"


def test_chat_explains_configuration_failure_without_a_run_context(tmp_path):
    supervisor = RunSupervisor()
    supervisor.attach(tmp_path)
    supervisor.record(
        EventType.CONFIGURATION_FAILED,
        status="failed",
        data=ConfigurationFailedData(
            code="config_load_failed",
            stage="config_loading",
            message="agent.toml was not found",
            usage=None,
            exit_code=2,
        ),
    )

    response = SupervisionService(supervisor).execute(ChatQuery(text="why did startup fail?"))

    assert "config_loading" in response.chat.answer
    assert "agent.toml was not found" in response.chat.answer


def test_run_context_chat_exposes_trajectory_without_inlining_it_in_prompt(tmp_path):
    supervisor = RunSupervisor()
    supervisor.attach(tmp_path / "logs")
    (tmp_path / "logs" / "progress.md").write_text("Round 2 improved throughput.")
    ctx = _RunContext.__new__(_RunContext)
    ctx.supervisor = supervisor
    ctx.agent_runner = Mock()
    ctx.agent_runner.invoke_text.return_value = "It improved in round 2."
    ctx._paths = RunPaths(
        exp_dir=tmp_path,
        log_dir=tmp_path / "logs",
        workspace=tmp_path / "workspace",
        run_log_path=tmp_path / "run.log",
    )
    ctx.gpu_env = lambda: {}
    ctx._progress_stack = []
    ctx._chat_lock = threading.Lock()
    ctx._chat_history = []
    ctx.logger = Mock()
    ctx.logger.file = Mock()

    answer = ctx.chat("what improved?")

    assert answer == "It improved in round 2."
    invocation = ctx.agent_runner.invoke_text.call_args.kwargs
    assert invocation["kind"] == "chat"
    assert "response_cls" not in invocation
    assert invocation["user_prompt"] == "what improved?"
    assert "Round 2 improved throughput." not in invocation["user_prompt"]
    assert "_vibesys_chat/trajectory/" in invocation["system_prompt"]
    assert "read-only investigation agent" in invocation["system_prompt"]
    trajectory = tmp_path / "workspace" / "_vibesys_chat" / "trajectory"
    assert (trajectory / "progress.md").read_text() == "Round 2 improved throughput."
    transcript = tmp_path / "workspace" / "_vibesys_chat" / "conversation.jsonl"
    assert json.loads(transcript.read_text()) == {
        "question": "what improved?",
        "answer": "It improved in round 2.",
    }

    ctx.agent_runner.invoke_text.side_effect = RuntimeError("agent unavailable")
    with pytest.raises(RuntimeError, match="Chat agent failed: RuntimeError: agent unavailable"):
        ctx.chat("what is the current status?")
    continuation = ctx.agent_runner.invoke_text.call_args.kwargs
    assert continuation["user_prompt"] == "what is the current status?"
    assert "It improved in round 2." not in continuation["user_prompt"]
    assert "_vibesys_chat/instructions.md" in continuation["system_prompt"]
    assert "Prefer targeted commands" not in continuation["system_prompt"]
    assert ctx._load_chat_history() == [("what improved?", "It improved in round 2.")]


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


def test_socket_transport_returns_clear_chat_agent_errors(tmp_path):
    supervisor = RunSupervisor()
    supervisor.attach(tmp_path / "logs")

    def fail_chat(question: str) -> str:
        raise RuntimeError(f"Chat agent failed while answering: {question}")

    supervisor.set_chat_handler(fail_chat)
    socket_path = Path("/tmp") / f"vibesys-test-{uuid.uuid4().hex}.sock"

    with SupervisionSocketServer(socket_path, SupervisionService(supervisor)):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(socket_path))
            stream = client.makefile("rwb")
            stream.write(ChatQuery(text="what happened?").model_dump_json().encode() + b"\n")
            stream.flush()
            response = json.loads(stream.readline())

    assert response["ok"] is False
    assert response["error"] == "Chat agent failed while answering: what happened?"


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
            "vibesys",
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
    chat_responses = []

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
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as chat_client:
                chat_client.settimeout(5)
                chat_client.connect(str(socket_path))
                chat_stream = chat_client.makefile("rwb")
                chat_stream.write(
                    ChatQuery(text="why did startup fail?").model_dump_json().encode() + b"\n"
                )
                chat_stream.flush()
                chat_responses.append(json.loads(chat_stream.readline()))
                chat_stream.close()
            stream.close()

    subscriber = threading.Thread(target=subscribe_until_failure)
    subscriber.start()
    failure = ConfigurationError(
        ConfigurationDiagnostic(
            code="invalid_arguments",
            stage="argument_parsing",
            message="unknown option --bad",
            usage="usage: vibesys ...",
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
            "usage": "usage: vibesys ...",
            "exit_code": 2,
        }
        assert not any(event["type"] == "run_failed" for event in received_events)
        assert chat_responses[0]["ok"] is True
        assert "unknown option --bad" in chat_responses[0]["chat"]["answer"]
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
    ctx._paths = RunPaths(
        exp_dir=tmp_path,
        log_dir=tmp_path / "logs",
        workspace=tmp_path,
        run_log_path=tmp_path / "run.log",
    )
    ctx.gpu_env = lambda: {}
    ctx._progress_stack = []

    result = ctx.invoke(
        kind="implementer",
        system_prompt="system",
        user_prompt="original",
        response_cls=dict,
        fallback_factory=dict,
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
