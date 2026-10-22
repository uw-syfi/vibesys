"""Session process behavior over real files and simulated scheduling, with no model calls."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.task_project import write_task

from entrypoints.chat_session import serve_pending
from vibesys.api import (
    AgentOutputChunkData,
    CoreEvent,
    CoreEventType,
    JsonResultPayload,
    RunReady,
    RunStatus,
    RunView,
    ToolResultData,
)
from vibesys.api.chat import ChatRuntime, ChatSession, SessionAction, SessionLifecycle, run_head
from vibesys.api.host import host_call
from vibesys.api.store import RunRecordFacts
from vibesys.api.testing import FakeAuxiliaryAgents, FakeManagedAgent, FakeRunReader, FakeRunRecord
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord
from vs_project.api.testing import run_execution_record
from vs_proto.api import (
    ChatMessageRequest,
    ChatSessionCreated,
    ChatSteerProposed,
    Ended,
    HostCallRequest,
    HostCallResponse,
    Lifecycle,
    Oneof,
    Outcome,
    Parked,
    RunFailure,
    RunFailureKind,
    RunHead,
    Running,
    RunProcess,
    SteerMode,
    SteerTarget,
    SteerTargetKind,
    Timestamp,
    validate,
)
from vs_runlog.api import RunLogReader, assert_run_unlocked
from vs_sim.api.testing import FakeProcesses, FakeProcessSignaller, ProcessScript, Sim, SimThreads

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def chat_call(
    payload: bytes, *, start_chat: Callable[[ChatMessageRequest], None] | None = None
) -> bytes:
    threads = SimThreads()
    processes = FakeProcesses(lambda _spec: ProcessScript(), pass_time=threads.sleep)
    response = host_call(
        payload,
        platform=replace(
            Sim(seed=0).platform(
                threads, processes=processes, signaller=FakeProcessSignaller(set())
            ),
            writer_signaller=None,
        ),
        start_chat=start_chat,
    )
    assert not processes.ran
    return response


@pytest.fixture
def chat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ChatMessageRequest:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    project = Project.open(root)
    project.state.create_project("chat", now=datetime(2026, 1, 1, tzinfo=UTC))
    manifest = project.state.new_run_manifest(
        "chat",
        branch="test",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(
            id="dynamic", config_version=1, options={"interface": "service", "max_rounds": 2}
        ),
        trusted_input_baseline="a" * 40,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    project.state.create_run(manifest)
    with project.state.exclusive_run_host(manifest.run_id):
        pass
    request = ChatMessageRequest(
        project_root=str(root),
        run_id=manifest.run_id,
        session_id="default",
        request_id="q-1",
        text="What happened?",
    )
    publish(request)
    return request


def publish(request: ChatMessageRequest, *, parked: bool = False, epoch: int = 1) -> None:
    head = RunHead(
        strategy="test",
        lifecycle=Lifecycle(
            state=Oneof("parked", Parked()) if parked else Oneof("running", Running())
        ),
        process=RunProcess(pid=1, epoch=epoch, started_at=Timestamp(seconds=1)),
        committed_seq=0,
    )
    validate(head)
    path = Project.run_log_path_for(request.project_root, request.run_id).parent
    path.mkdir(parents=True, exist_ok=True)
    (path / "run.head").write_bytes(head.to_binary())


def post(request: ChatMessageRequest) -> None:
    project = Project.open(request.project_root)
    project.state.session_inbox(request.run_id, request.session_id).post(
        request.request_id, request.to_binary(), ended=lambda: None
    )


def events(request: ChatMessageRequest) -> list:
    path = (
        Project.chat_session_directory_for(request.project_root, request.run_id, request.session_id)
        / "session.log"
    )
    with path.open("rb") as stream:
        records = list(RunLogReader(stream))
    assert [r.seq for r in records] == list(range(1, len(records) + 1))
    for record in records:
        validate(record)
    head = RunHead.from_binary((path.parent / "run.head").read_bytes())
    assert head.committed_seq == len(records)
    result = []
    for record in records:
        assert record.body is not None
        assert record.body.field == "chat"
        result.append(record.body.value.event)
    return result


def run(
    request: ChatMessageRequest, respond: Callable[[str, str | None], str], *, idle: float = 0.2
) -> FakeManagedAgent:
    threads = SimThreads()
    agent = FakeManagedAgent(respond)

    def serve() -> None:
        session = ChatSession(request, ChatRuntime(threads.clock, threads, idle_seconds=idle))
        try:
            session.serve(
                ChatSessionCreated(id=request.session_id, provider="test", model="test"),
                lambda: agent,
            )
        finally:
            session.close()

    threads.run(serve)
    return agent


def test_start_on_demand_idle_exit_and_relaunch_preserve_history(chat: ChatMessageRequest) -> None:
    directory = Project.chat_session_directory_for(chat.project_root, chat.run_id, chat.session_id)
    assert not directory.exists()
    starts: list[ChatMessageRequest] = []
    payload = HostCallRequest(
        checkout="/src/vibesys", method=Oneof("chat_message", chat)
    ).to_binary()
    response = HostCallResponse.from_binary(chat_call(payload, start_chat=starts.append))
    validate(response)
    assert response.result is not None
    assert response.result.field == "chat_message"
    assert starts == [chat]
    # Retrying admission is still one ordered inbox message.
    assert chat_call(payload, start_chat=starts.append) == response.to_binary()
    agent = run(chat, lambda text, _invocation: f"answer to {text}")
    assert agent.closed
    assert len(agent.messages) == 1
    assert_run_unlocked(directory / "session.log")
    prior = (directory / "session.log").read_bytes()
    assert len(events(chat)) == 3
    publish(chat, parked=True)
    second = ChatMessageRequest.from_binary(chat.to_binary())
    second.request_id, second.text = "q-2", "Why?"
    post(second)
    parked = run(second, lambda text, _invocation: text)
    assert parked.messages == []
    assert (directory / "session.log").read_bytes() == prior
    publish(chat, epoch=2)
    restored = run(second, lambda _text, _invocation: "continued")
    prompt = json.loads(restored.messages[0][0])
    assert prompt["question"] == "Why?"
    assert json.loads(prompt["untrusted_prior_chat_transcript_jsonl"])["question"] == chat.text
    assert (directory / "session.log").read_bytes().startswith(prior)
    assert len(events(second)) == 5


def test_tokens_and_proposals_are_session_data_and_never_intents(chat: ChatMessageRequest) -> None:
    threads = SimThreads()
    post(chat)

    def serve() -> None:
        session = ChatSession(chat, ChatRuntime(threads.clock, threads, idle_seconds=0))

        def answer(_text: str, _invocation: str | None) -> str:
            session.token("hello ")
            session.token("world")
            session.propose(
                ChatSteerProposed(
                    id="proposal",
                    text="try this",
                    target=SteerTarget(kind=SteerTargetKind.RUN),
                    mode=SteerMode.NEXT_TURN,
                )
            )
            publish(chat, parked=True)
            return "hello world"

        try:
            session.serve(
                ChatSessionCreated(id="default", provider="test", model="test"),
                lambda: FakeManagedAgent(answer),
            )
        finally:
            session.close()

    threads.run(serve)
    observed = events(chat)
    assert not observed[-1].value.failed, observed[-1].value
    assert [event.field for event in observed] == [
        "created",
        "message",
        "token",
        "token",
        "proposed_steer",
        "message",
    ]
    assert observed[-2].value.request_id == chat.request_id
    assert Project.open(chat.project_root).state.control_inbox(chat.run_id).pending() == ()


def test_durable_answer_recovers_before_inbox_settlement(chat: ChatMessageRequest) -> None:
    post(chat)
    run(chat, lambda _text, _invocation: "answer")
    # A retry after an uncertain acknowledgement cannot repeat an already durable turn.
    post(chat)
    again = run(chat, lambda _text, _invocation: "should not run")
    assert again.messages == []
    assert len(events(chat)) == 3


@given(
    now=st.floats(min_value=0, max_value=10000, allow_nan=False, allow_infinity=False),
    pending=st.booleans(),
    parked=st.booleans(),
)
def test_lifecycle_has_no_idle_race(now: float, *, pending: bool, parked: bool) -> None:
    state = SessionLifecycle(0, 1, idle_seconds=10)
    action = state.next(now, 1, parked=parked, pending=pending)
    if parked:
        assert action is SessionAction.EXIT
    elif pending:
        assert action is SessionAction.ANSWER
    else:
        assert action is (SessionAction.EXIT if now >= 10 else SessionAction.WAIT)
    assert state.next(now, 2, parked=False, pending=pending) is SessionAction.EXIT


def test_conflicting_id_and_missing_launcher_are_typed_failures(chat: ChatMessageRequest) -> None:
    payload = HostCallRequest(
        checkout="/src/vibesys", method=Oneof("chat_message", chat)
    ).to_binary()
    unavailable = HostCallResponse.from_binary(chat_call(payload))
    assert unavailable.result is not None
    assert unavailable.result.field == "configuration_failed"
    assert unavailable.result.value.code == "unavailable"
    admitted = HostCallResponse.from_binary(
        chat_call(
            payload,
            start_chat=lambda _request: None,
        )
    )
    assert admitted.result is not None
    assert admitted.result.field == "chat_message"
    chat.text = "changed"
    payload = HostCallRequest(
        checkout="/src/vibesys", method=Oneof("chat_message", chat)
    ).to_binary()
    refused = HostCallResponse.from_binary(
        chat_call(
            payload,
            start_chat=lambda _request: None,
        )
    )
    assert refused.result is not None
    assert refused.result.field == "configuration_failed"


def test_stream_callbacks_validate_proposals_and_keep_large_answers(
    chat: ChatMessageRequest,
) -> None:

    threads = SimThreads()
    post(chat)
    answer = "evidence " * 10000

    def serve() -> None:
        session = ChatSession(chat, ChatRuntime(threads.clock, threads, idle_seconds=0))
        session.token("unowned")

        def respond(_text: str, invocation: str | None) -> str:
            session.observe(
                CoreEvent(
                    type=CoreEventType.AGENT_OUTPUT_CHUNK,
                    timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                    execution_id="previous-question",
                    data=AgentOutputChunkData(channel="assistant", content="obsolete"),
                )
            )
            session.observe(
                CoreEvent(
                    type=CoreEventType.AGENT_OUTPUT_CHUNK,
                    timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                    execution_id=invocation,
                    data=AgentOutputChunkData(channel="assistant", content="answer"),
                )
            )
            for value in [
                [],
                {"kind": "steer_proposal"},
                {
                    "kind": "steer_proposal",
                    "proposal": {"text": "compare", "target": {"kind": "run"}, "mode": "next_turn"},
                    "note": "Operator decides.",
                },
            ]:
                session.observe(
                    CoreEvent(
                        type=CoreEventType.TOOL_RESULT,
                        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                        execution_id=invocation,
                        data=ToolResultData(
                            tool="propose_steer",
                            call_id="tool-a",
                            content="result",
                            payload=JsonResultPayload(value=value),
                        ),
                    )
                )
            return answer

        agent = FakeManagedAgent(respond)
        try:
            session.serve(
                ChatSessionCreated(id="default", provider="test", model="test"), lambda: agent
            )
        finally:
            session.close()

    threads.run(serve)
    observed = events(chat)
    assert not observed[-1].value.failed, observed[-1].value
    assert [event.field for event in observed] == [
        "created",
        "message",
        "token",
        "proposed_steer",
        "message",
    ]
    blob = observed[-1].value.content.value
    directory = Project.chat_session_directory_for(chat.project_root, chat.run_id, chat.session_id)
    assert (directory / "blobs" / blob.sha256).read_text() == answer
    # Restoration keeps complete bounded exchanges; a larger single turn is omitted whole.
    second = ChatMessageRequest.from_binary(chat.to_binary())
    second.request_id = "q-2"
    post(second)
    restored = run(second, lambda text, _invocation: text)
    assert restored.messages[0][0] == chat.text


def test_failed_turn_is_durable_and_does_not_repeat_on_retry(chat: ChatMessageRequest) -> None:
    post(chat)

    def failed(_text: str, _invocation: str | None) -> str:
        message = "model unavailable"
        raise RuntimeError(message)

    run(chat, failed)
    observed = events(chat)
    assert observed[-1].value.failed
    assert observed[-1].value.content.value == "model unavailable"
    post(chat)
    assert run(chat, failed).messages == []


def test_invalid_inbox_routes_are_rejected_without_answering(chat: ChatMessageRequest) -> None:
    inbox = Project.open(chat.project_root).state.session_inbox(chat.run_id, chat.session_id)
    invalid = ChatMessageRequest.from_binary(chat.to_binary())
    invalid.session_id = "other"
    inbox.post(invalid.request_id, invalid.to_binary(), ended=lambda: None)
    inbox.post("invalid", b"not protobuf", ended=lambda: None)
    assert run(chat, lambda text, _invocation: text).messages == []
    assert inbox.pending() == ()
    status = inbox.status(invalid.request_id, ended=None)
    assert status is not None
    assert status.reason == "chat message routing does not match its inbox"


def test_reading_an_incomplete_native_head_is_refused(chat: ChatMessageRequest) -> None:
    path = Project.run_log_path_for(chat.project_root, chat.run_id).parent / "run.head"
    head = RunHead.from_binary(path.read_bytes())
    head.process = None
    path.write_bytes(head.to_binary())
    with pytest.raises(ValueError, match="process and lifecycle"):
        run_head(path)


@pytest.mark.parametrize("session_id", ["default", "Research_A", "chat2"])
def test_session_ids_follow_the_host_contract(chat: ChatMessageRequest, session_id: str) -> None:
    chat.session_id = session_id
    response = HostCallResponse.from_binary(
        chat_call(
            HostCallRequest(
                checkout="/src/vibesys", method=Oneof("chat_message", chat)
            ).to_binary(),
            start_chat=post,
        )
    )
    assert response.result is not None
    assert response.result.field == "chat_message"
    assert response.result.value.session_id == session_id
    run(chat, lambda text, _invocation: text)
    assert len(events(chat)) == 3


def test_process_refreshes_after_relaunch_without_stranding_admitted_messages(
    chat: ChatMessageRequest,
) -> None:
    project = Project.open(chat.project_root)
    write_task(
        project.root,
        'version = 1\n[agent]\ndomain = "generic"\n[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n',
        objective="Make the queue faster.\n",
    )
    ready = RunReady(
        record=FakeRunRecord(
            chat.run_id,
            RunView(run_id=chat.run_id, loop="dynamic", status=RunStatus.ACTIVE),
            RunRecordFacts(trusted_input_baseline="a" * 40),
        ),
        project_root=project.root,
        log_directory=Project.run_log_path_for(chat.project_root, chat.run_id).parent,
        frontend_state_directory=project.root / "frontend",
        agent_provider="codex",
        agent_model="gpt-test",
        agent_providers=("codex",),
    )
    post(chat)
    readers: list[FakeRunReader] = []
    agents: list[FakeManagedAgent] = []

    def open_reader(request: object) -> FakeRunReader:
        del request

        def respond(text: str, _invocation: str | None) -> str:
            if len(readers) == 1:
                publish(chat, epoch=2)
                second = ChatMessageRequest.from_binary(chat.to_binary())
                second.request_id, second.text = "q-2", "Why?"
                post(second)
            return text

        agent = FakeManagedAgent(respond)
        agents.append(agent)
        reader = FakeRunReader(ready, FakeAuxiliaryAgents(lambda _launch: agent))
        readers.append(reader)
        return reader

    threads = SimThreads()
    threads.run(
        lambda: serve_pending(
            chat, ChatRuntime(threads.clock, threads, idle_seconds=0), open_reader
        )
    )
    assert len(readers) == 2
    assert all(reader.closed for reader in readers)
    assert all(agent.closed for agent in agents)
    assert agents[0].messages[0][0] == chat.text
    assert json.loads(agents[1].messages[0][0])["question"] == "Why?"
    assert len(events(chat)) == 5
    scope = readers[0].agents
    assert isinstance(scope, FakeAuxiliaryAgents)
    assert scope.closed
    assert scope.launches[0].readable_inputs[0].path == ready.log_directory
    assert readers[0].sinks[0] is not None
    with pytest.raises(RuntimeError, match="scope is closed"):
        scope.create_auxiliary_agent(scope.launches[0])
    with pytest.raises(RuntimeError, match="conversation is closed"):
        agents[0].turn("late")


@pytest.mark.parametrize(
    ("outcome", "failure"),
    [
        (Outcome.FINISHED, None),
        (Outcome.STOPPED, None),
        (Outcome.INTERRUPTED, None),
        *[
            (Outcome.FAILED, kind)
            for kind in RunFailureKind
            if kind is not RunFailureKind.UNSPECIFIED
        ],
    ],
)
def test_every_ending_uses_the_same_chat_admission_and_retained_history(
    chat: ChatMessageRequest, outcome: Outcome, failure: RunFailureKind | None
) -> None:
    path = Project.run_head_path_for(chat.project_root, chat.run_id)
    head = RunHead.from_binary(path.read_bytes())
    head.lifecycle = Lifecycle(
        state=Oneof(
            "ended",
            Ended(
                outcome=outcome,
                resumable=True,
                failure=None
                if failure is None
                else RunFailure(kind=failure, reason="Recorded ending"),
            ),
        )
    )
    validate(head)
    original = head.to_binary()
    path.write_bytes(original)
    starts: list[ChatMessageRequest] = []
    for index in range(2):
        question = ChatMessageRequest.from_binary(chat.to_binary())
        question.request_id, question.text = f"q-{index + 1}", f"Question {index + 1}"
        response = HostCallResponse.from_binary(
            chat_call(
                HostCallRequest(
                    checkout="/src/checkout", method=Oneof("chat_message", question)
                ).to_binary(),
                start_chat=starts.append,
            )
        )
        validate(response)
        assert response.result is not None
        assert response.result.field == "chat_message"
        assert response.result.value.request_id == question.request_id
        agent = run(question, lambda text, _invocation: f"answer to {text}")
        if index:
            prompt = json.loads(agent.messages[0][0])
            assert prompt["question"] == question.text
            prior = json.loads(prompt["untrusted_prior_chat_transcript_jsonl"])
            assert prior["question"] == "Question 1"
        else:
            assert agent.messages[0][0] == question.text
        assert path.read_bytes() == original
    assert len(starts) == 2
    assert len(events(chat)) == 5
