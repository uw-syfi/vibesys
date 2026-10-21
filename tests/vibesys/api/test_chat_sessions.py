"""Session process behavior over real files and simulated scheduling, with no model calls."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vibesys.api.chat import ChatRuntime, ChatSession, SessionAction, SessionLifecycle
from vibesys.api.host import host_call
from vibesys.api.testing import FakeManagedAgent
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord
from vs_project.api.testing import run_execution_record
from vs_proto.api import (
    ChatMessageRequest,
    ChatSessionCreated,
    ChatSteerProposed,
    HostCallRequest,
    HostCallResponse,
    Lifecycle,
    Oneof,
    Parked,
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
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


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
        orchestration=OrchestrationDescriptor(id="dynamic", config_version=1, options={}),
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
    response = HostCallResponse.from_binary(host_call(payload, start_chat=starts.append))
    validate(response)
    assert response.result is not None
    assert response.result.field == "chat_message"
    assert starts == [chat]
    # Retrying admission is still one ordered inbox message.
    assert host_call(payload, start_chat=starts.append) == response.to_binary()
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
    unavailable = HostCallResponse.from_binary(host_call(payload))
    assert unavailable.result is not None
    assert unavailable.result.field == "configuration_failed"
    assert unavailable.result.value.code == "unavailable"
    admitted = HostCallResponse.from_binary(host_call(payload, start_chat=lambda _request: None))
    assert admitted.result is not None
    assert admitted.result.field == "chat_message"
    chat.text = "changed"
    payload = HostCallRequest(
        checkout="/src/vibesys", method=Oneof("chat_message", chat)
    ).to_binary()
    refused = HostCallResponse.from_binary(host_call(payload, start_chat=lambda _request: None))
    assert refused.result is not None
    assert refused.result.field == "configuration_failed"
