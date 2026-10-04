"""One public durable-session contract suite for Client and Fake implementations."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from threading import Event
from typing import TYPE_CHECKING

import pytest
from agentshim.testing import FakeExecutor, FakeRun, scripted_turn
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vs_agent.api import (
    AgentCapabilities,
    AgentClient,
    AgentExecutionPolicy,
    AgentInvocationRecord,
    AgentInvocationState,
    AgentSessionKey,
    AgentSessionSpec,
    AgentSessionState,
    AgentTurnRequest,
    ClientAgentSessions,
    Completed,
    DurableSessionStore,
    InvocationConflictError,
    Pending,
    SessionConfigurationError,
    SessionPersistenceError,
    SessionResumeError,
    SessionScope,
    Unknown,
)
from vs_agent.api.testing import (
    FakeAgentInvocationStore,
    FakeAgentSessions,
    FakeDriver,
    fake_agentshim_driver,
)
from vs_project.api import (
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
    RunExecutionRecord,
)
from vs_prompts.api import TemplateRenderer

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_agent.api import AgentInvocationStore
    from vs_project.api import StateNamespace
    from vs_prompts.api import RenderedPrompt


KEY = AgentSessionKey(SessionScope.HYPOTHESIS, "H-01")


def _namespace(root: Path) -> StateNamespace:
    project = Project.open(root)
    project.state.create_project("session-contract")
    run = project.state.new_run_manifest(
        "session-contract",
        run_id="run-1",
        branch="test",
        vibesys_version="test",
        trusted_input_baseline="a" * 40,
        run_environment=RunEnvironmentRecord(name="local"),
        execution=RunExecutionRecord(
            model="test",
            agent_backend="stub",
            compute_backend="cpu",
            requested_profiler="none",
            resolved_profiler="none",
            agent_roles={},
        ),
        orchestration=OrchestrationDescriptor(id="test", config_version=1, options={}),
    )
    project.state.create_run(run)
    return project.state.local_namespace(run.run_id, "agent")


@dataclass
class _Boundary:
    calls: int = 0
    effect: Callable[[], None] | None = None
    failure_run: FakeRun | None = None

    def accept(self) -> None:
        self.calls += 1
        if self.calls > 1 and self.effect is not None:
            self.effect()


@dataclass
class _Harness:
    implementation: str
    client: AgentClient
    sessions: ClientAgentSessions
    ledger: AgentInvocationStore
    boundary: _Boundary
    spec: AgentSessionSpec
    turn: AgentTurnRequest
    message: RenderedPrompt
    namespace: StateNamespace
    new_client: Callable[[], AgentClient]

    def reconstruct(self) -> ClientAgentSessions:
        factory = FakeAgentSessions if self.implementation == "fake" else ClientAgentSessions
        sessions = factory(self.client, self.ledger)
        sessions.bind(KEY, self.spec, self.turn)
        return sessions


@pytest.fixture(params=["fake", "client"])
def harness(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> _Harness:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "state"))
    tmp_path = tmp_path / "workspace"
    tmp_path.mkdir()
    namespace = _namespace(tmp_path)
    checkpoints = DurableSessionStore(
        namespace.slot(
            "sessions.json",
            AgentSessionState,
        )
    )
    boundary = _Boundary()
    if request.param == "fake":
        driver = FakeDriver(answer="done", on_turn=lambda _: boundary.accept())
    else:

        def execute(_request: object) -> FakeRun:
            boundary.accept()
            return boundary.failure_run or scripted_turn(
                "codex", session_id="thread-1", text="done"
            )

        driver = fake_agentshim_driver(provider="codex", executor=FakeExecutor(execute))

    def new_client() -> AgentClient:
        return AgentClient(driver, provider="codex", session_store=checkpoints)

    client = new_client()
    spec = AgentSessionSpec(
        role="implementer",
        provider="codex",
        workspace=tmp_path,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    client.run(session_spec=spec, turn=AgentTurnRequest(message="first"), session_key=KEY)
    turn = AgentTurnRequest(
        message="", instructions="", expected_provider_session_id=client.provider_session_id(KEY)
    )
    ledger = (
        FakeAgentInvocationStore()
        if request.param == "fake"
        else namespace.slot("invocations.json", AgentInvocationState)
    )
    factory = FakeAgentSessions if request.param == "fake" else ClientAgentSessions
    sessions = factory(client, ledger)
    sessions.bind(KEY, spec, turn)
    (tmp_path / "message.j2").write_text("{{ content }}", encoding="utf-8")
    message = TemplateRenderer(tmp_path).render_template("message.j2", content="trusted results")
    yield _Harness(
        request.param,
        client,
        sessions,
        ledger,
        boundary,
        spec,
        turn,
        message,
        namespace,
        new_client,
    )
    client.close()


def test_resume_continues_checkpoint_once_and_replays_durable_result(harness: _Harness) -> None:
    checkpoint = harness.sessions.checkpoint(KEY)
    result = harness.sessions.resume(KEY, harness.message, "resume-1")
    assert isinstance(result, Completed)
    assert result.checkpoint == checkpoint
    assert result.result.provider_session_id == checkpoint.provider_session_id
    assert harness.sessions.inspect(KEY, "resume-1") == result
    assert harness.reconstruct().resume(KEY, harness.message, "resume-1") == result
    assert harness.boundary.calls == 2


def test_reused_identity_with_changed_payload_is_conflict(harness: _Harness) -> None:
    harness.sessions.resume(KEY, harness.message, "resume-1")
    changed = TemplateRenderer(harness.spec.workspace).render_template(
        "message.j2", content="different"
    )
    with pytest.raises(InvocationConflictError, match="payload changed"):
        harness.sessions.resume(KEY, changed, "resume-1")
    other = AgentSessionKey(SessionScope.HYPOTHESIS, "H-02")
    with pytest.raises(InvocationConflictError, match="another key"):
        harness.sessions.inspect(other, "resume-1")
    assert harness.boundary.calls == 2


def test_missing_and_recovered_unfinished_invocation_is_unknown(harness: _Harness) -> None:
    assert isinstance(harness.sessions.inspect(KEY, "absent"), Unknown)
    entered, release, ready = Event(), Event(), Event()

    def block() -> None:
        entered.set()
        ready.set()
        release.wait()

    harness.boundary.effect = block
    with ThreadPoolExecutor(max_workers=1) as workers:
        result = workers.submit(harness.sessions.resume, KEY, harness.message, "resume-1")
        result.add_done_callback(lambda _: ready.set())
        try:
            ready.wait()
            assert entered.is_set(), result.result()
            assert isinstance(harness.sessions.inspect(KEY, "resume-1"), Pending)
            assert isinstance(harness.sessions.resume(KEY, harness.message, "resume-1"), Pending)
            recovered = harness.reconstruct()
            assert isinstance(recovered.inspect(KEY, "resume-1"), Unknown)
            recovered_outcome = recovered.resume(KEY, harness.message, "resume-1")
            assert isinstance(recovered_outcome, Unknown)
            assert recovered_outcome.checkpoint == harness.sessions.checkpoint(KEY)
            with pytest.raises(InvocationConflictError, match="unresolved invocation"):
                recovered.resume(KEY, harness.message, "resume-2")
            with pytest.raises(InvocationConflictError, match="active invocation"):
                harness.sessions.resume(KEY, harness.message, "resume-2")
            assert harness.boundary.calls == 2
        finally:
            release.set()
    assert isinstance(result.result(), Completed)


def test_unexpected_boundary_failure_is_unknown_and_never_replayed(harness: _Harness) -> None:
    def fail() -> None:
        detail = "lost acknowledgement"
        raise LookupError(detail)

    harness.boundary.effect = fail
    result = harness.sessions.resume(KEY, harness.message, "resume-1")
    assert isinstance(result, Unknown)
    assert "LookupError" in result.detail
    assert harness.reconstruct().resume(KEY, harness.message, "resume-1") == result
    with pytest.raises(InvocationConflictError, match="unresolved invocation"):
        harness.reconstruct().resume(KEY, harness.message, "resume-2")
    assert harness.boundary.calls == 2


def test_cancellation_persists_unknown_before_propagating(harness: _Harness) -> None:
    def cancel() -> None:
        raise KeyboardInterrupt

    harness.boundary.effect = cancel
    with pytest.raises(KeyboardInterrupt):
        harness.sessions.resume(KEY, harness.message, "resume-1")
    assert isinstance(harness.sessions.inspect(KEY, "resume-1"), Unknown)
    assert isinstance(harness.reconstruct().resume(KEY, harness.message, "resume-1"), Unknown)
    assert harness.boundary.calls == 2


def test_missing_checkpoint_refuses_dispatch(harness: _Harness) -> None:
    missing = AgentSessionKey(SessionScope.HYPOTHESIS, "missing")
    harness.sessions.bind(missing, harness.spec, harness.turn)
    with pytest.raises(SessionResumeError, match="missing"):
        harness.sessions.resume(missing, harness.message, "resume-1")
    assert harness.boundary.calls == 1


def test_changed_bound_configuration_and_checkpoint_never_start_fresh(harness: _Harness) -> None:
    drift = replace(harness.spec, model="different")
    sessions = ClientAgentSessions(harness.client, harness.ledger)
    sessions.bind(KEY, drift, harness.turn)
    result = sessions.resume(KEY, harness.message, "resume-1")
    assert isinstance(result, Unknown)
    assert "specification changed" in result.detail
    assert harness.boundary.calls == 1


def test_nondurable_key_is_configuration_error(harness: _Harness) -> None:
    with pytest.raises(SessionConfigurationError, match="not durable"):
        harness.sessions.bind(
            AgentSessionKey(SessionScope.ROLE, "judge"), harness.spec, harness.turn
        )
    assert harness.boundary.calls == 1


def test_malformed_ledger_fails_typed_before_dispatch(harness: _Harness) -> None:
    slot = harness.namespace.slot("malformed.json", AgentInvocationState)
    harness.namespace.write_bytes("malformed.json", b'{"schema_version":2}')
    sessions = ClientAgentSessions(harness.client, slot)
    sessions.bind(KEY, harness.spec, harness.turn)
    with pytest.raises(SessionPersistenceError, match="read invocation ledger"):
        sessions.resume(KEY, harness.message, "resume-1")
    assert harness.boundary.calls == 1


@given(st.text(min_size=1))
def test_ledger_rejects_mismatched_invocation_id(identity: str) -> None:
    outcome = Pending(session_key=str(KEY), invocation_id=identity)
    record = AgentInvocationRecord(payload_digest="digest", outcome=outcome)
    with pytest.raises(ValidationError, match="invocation_id"):
        AgentInvocationState(invocations={identity + "x": record})


def test_declared_provider_resume_failure_never_falls_back(harness: _Harness) -> None:
    if harness.implementation == "client":
        harness.boundary.failure_run = FakeRun(stderr=["session not found\n"], returncode=1)
    else:

        def fail() -> None:
            raise SessionResumeError(str(KEY), "provider refused resume")

        harness.boundary.effect = fail
    result = harness.sessions.resume(KEY, harness.message, "resume-1")
    assert isinstance(result, Unknown)
    assert harness.boundary.calls == 2
    assert harness.reconstruct().resume(KEY, harness.message, "resume-1") == result


class _NoResumeDriver(FakeDriver):
    @property
    def capabilities(self) -> AgentCapabilities:
        return replace(super().capabilities, provider_session_resume=False)


def test_unsupported_provider_is_typed_preflight_error() -> None:
    client = AgentClient(_NoResumeDriver(answer="done"))
    try:
        with pytest.raises(SessionConfigurationError, match="provider_session_resume"):
            ClientAgentSessions(client, FakeAgentInvocationStore())
    finally:
        client.close()


class _CommitFailureStore(FakeAgentInvocationStore):
    def __init__(self, failing_save: int) -> None:
        super().__init__()
        self.saves = 0
        self.failing_save = failing_save

    def save(self, model: AgentInvocationState) -> None:
        self.saves += 1
        if self.saves == self.failing_save:
            detail = "injected atomic commit failure"
            raise OSError(detail)
        super().save(model)


@pytest.mark.parametrize("failing_save", [1, 2])
def test_commit_failure_never_authorizes_unsafe_replay(
    harness: _Harness, failing_save: int
) -> None:
    store = _CommitFailureStore(failing_save)
    sessions = ClientAgentSessions(harness.client, store)
    sessions.bind(KEY, harness.spec, harness.turn)
    with pytest.raises(SessionPersistenceError, match="commit invocation"):
        sessions.resume(KEY, harness.message, "resume-1")
    assert harness.boundary.calls == failing_save
    recovered = ClientAgentSessions(harness.client, store)
    recovered.bind(KEY, harness.spec, harness.turn)
    assert isinstance(recovered.inspect(KEY, "resume-1"), Unknown)
    if failing_save == 2:
        assert isinstance(recovered.resume(KEY, harness.message, "resume-1"), Unknown)
        assert harness.boundary.calls == 2


def test_new_client_adopts_durable_provider_checkpoint(harness: _Harness) -> None:
    client = harness.new_client()
    try:
        sessions = ClientAgentSessions(client, harness.ledger)
        sessions.bind(KEY, harness.spec, harness.turn)
        result = sessions.resume(KEY, harness.message, "resume-1")
        assert isinstance(result, Completed)
        assert result.checkpoint.provider_session_id == harness.turn.expected_provider_session_id
        assert harness.boundary.calls == 2
    finally:
        client.close()


def test_new_client_configuration_drift_refuses_adoption(harness: _Harness) -> None:
    client = harness.new_client()
    try:
        sessions = ClientAgentSessions(client, harness.ledger)
        sessions.bind(KEY, replace(harness.spec, model="changed"), harness.turn)
        outcome = sessions.resume(KEY, harness.message, "resume-1")
        assert isinstance(outcome, Unknown)
        assert "specification changed" in outcome.detail
        assert harness.boundary.calls == 1
    finally:
        client.close()


@pytest.mark.parametrize("scope", [None, SessionScope.ROLE])
def test_raw_strict_turn_cannot_start_ephemeral_or_nondurable_session(
    harness: _Harness, scope: SessionScope | None
) -> None:
    key = None if scope is None else AgentSessionKey(scope, "role")
    with pytest.raises(SessionResumeError, match="durable session key"):
        harness.client.run(session_spec=harness.spec, turn=harness.turn, session_key=key)
    assert harness.boundary.calls == 1


def test_agentshim_strict_turn_cannot_dispatch_without_adoption(tmp_path: Path) -> None:
    executor = FakeExecutor(scripted_turn("codex", session_id="new-thread", text="done"))
    driver = fake_agentshim_driver(provider="codex", executor=executor)
    spec = AgentSessionSpec(
        role="implementer",
        provider="codex",
        workspace=tmp_path,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    session = driver.create_session(spec)
    try:
        with pytest.raises(SessionResumeError, match="adopted"):
            session.run_turn(
                AgentTurnRequest(message="continue", expected_provider_session_id="old-thread")
            )
        assert executor.requests == []
    finally:
        session.close()
        driver.close()
