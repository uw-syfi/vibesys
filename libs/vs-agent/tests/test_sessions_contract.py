"""One public durable-session contract suite for Client and Fake implementations."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from functools import partial
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
    AgentOutputSchemaError,
    AgentSessionKey,
    AgentSessionSpec,
    AgentSessionState,
    AgentTurnRequest,
    ClientAgentSessions,
    Completed,
    DurableSessionStore,
    InvalidResponse,
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
    from collections.abc import Callable, Iterator
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
) -> Iterator[_Harness]:
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
            with pytest.raises(InvocationConflictError, match="active"):
                harness.sessions.release_interrupted(KEY, "resume-1")
            assert isinstance(harness.sessions.resume(KEY, harness.message, "resume-1"), Pending)
            recovered = harness.reconstruct()
            assert isinstance(recovered.inspect(KEY, "resume-1"), Unknown)
            recovered_outcome = recovered.resume(KEY, harness.message, "resume-1")
            assert isinstance(recovered_outcome, Unknown)
            assert recovered_outcome.checkpoint == harness.sessions.checkpoint(KEY)
            with pytest.raises(SessionResumeError, match="unfinished dispatch recovered"):
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
    with pytest.raises(SessionResumeError, match="LookupError: lost acknowledgement"):
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


@pytest.mark.parametrize("implementation", ["fake", "agentshim"])
def test_strict_turn_cannot_dispatch_without_adoption(tmp_path: Path, implementation: str) -> None:
    fake_calls: list[AgentTurnRequest] = []
    executor = FakeExecutor(scripted_turn("codex", session_id="new-thread", text="done"))
    driver = (
        FakeDriver(answer="done", on_turn=fake_calls.append)
        if implementation == "fake"
        else fake_agentshim_driver(provider="codex", executor=executor)
    )
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
        if implementation == "agentshim":
            assert executor.requests == []
        else:
            assert fake_calls == []
    finally:
        session.close()
        driver.close()


@pytest.mark.parametrize("implementation", ["fake", "agentshim"])
@pytest.mark.parametrize("scenario", ["adopted", "adoption-mismatch", "prior-turn"])
def test_strict_turn_identity_matches_adoption_and_prior_turn(
    tmp_path: Path, implementation: str, scenario: str
) -> None:
    fake_calls: list[AgentTurnRequest] = []
    executor = FakeExecutor(
        lambda request: scripted_turn(
            "codex",
            session_id="old-thread" if "old-thread" in request.argv else "thread-1",
            text="done",
        )
    )
    driver = (
        FakeDriver(answer="done", on_turn=fake_calls.append)
        if implementation == "fake"
        else fake_agentshim_driver(provider="codex", executor=executor)
    )
    spec = AgentSessionSpec(
        role="implementer",
        provider="codex",
        workspace=tmp_path,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    session = driver.create_session(spec)
    try:
        if scenario == "prior-turn":
            initial = session.run_turn(AgentTurnRequest(message="first"))
            expected = initial.provider_session_id
            assert expected is not None
            assert not session.resume_provider_session("stale-thread")
        else:
            assert session.resume_provider_session("old-thread")
            expected = "old-thread"
        if scenario == "adoption-mismatch":
            with pytest.raises(SessionResumeError, match="adopted"):
                session.run_turn(
                    AgentTurnRequest(message="wrong", expected_provider_session_id="other")
                )
            if implementation == "fake":
                assert fake_calls == []
            else:
                assert executor.requests == []
        result = session.run_turn(
            AgentTurnRequest(message="continue", expected_provider_session_id=expected)
        )
        assert result.provider_session_id == expected
        assert result.text == "done"
        if implementation == "fake":
            assert len(fake_calls) == (2 if scenario == "prior-turn" else 1)
        else:
            assert len(executor.requests) == (2 if scenario == "prior-turn" else 1)
    finally:
        session.close()
        driver.close()


@pytest.mark.parametrize("implementation", ["fake", "agentshim"])
def test_session_cannot_adopt_while_turn_is_in_flight(tmp_path: Path, implementation: str) -> None:
    entered = Event()
    release = Event()

    def block_turn(_request: object) -> FakeRun:
        entered.set()
        release.wait()
        return scripted_turn("codex", session_id="thread-1", text="done")

    def block_fake_turn(_request: AgentTurnRequest) -> None:
        entered.set()
        release.wait()

    driver = (
        FakeDriver(answer="done", on_turn=block_fake_turn)
        if implementation == "fake"
        else fake_agentshim_driver(provider="codex", executor=FakeExecutor(block_turn))
    )
    spec = AgentSessionSpec(
        role="implementer",
        provider="codex",
        workspace=tmp_path,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    session = driver.create_session(spec)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            turn = pool.submit(session.run_turn, AgentTurnRequest(message="first"))
            try:
                entered.wait()
                assert not session.resume_provider_session("late-thread")
            finally:
                release.set()
            assert turn.result().text == "done"
    finally:
        release.set()
        session.close()
        driver.close()


@pytest.mark.parametrize("kind", [Pending, Unknown])
def test_checkpoint_key_must_match_unfinished_invocation(
    kind: type[Pending] | type[Unknown],
) -> None:
    from_key = str(KEY)
    document = {
        "session_key": from_key,
        "invocation_id": "resume-1",
        "checkpoint": {"session_key": "hypothesis:H-02", "provider_session_id": "thread"},
    }
    if kind is Unknown:
        document["detail"] = "ambiguous"
    with pytest.raises(ValidationError, match="checkpoint session_key"):
        kind.model_validate(document)


@pytest.mark.parametrize("method", ["inspect", "resume"])
def test_empty_invocation_id_is_typed_configuration_error(harness: _Harness, method: str) -> None:
    call = (
        partial(harness.sessions.inspect, KEY, "")
        if method == "inspect"
        else partial(harness.sessions.resume, KEY, harness.message, "")
    )
    with pytest.raises(SessionConfigurationError, match="invocation_id"):
        call()
    assert harness.boundary.calls == 1


def test_inspection_rejects_nondurable_key_before_loading_ledger(harness: _Harness) -> None:
    key = AgentSessionKey(SessionScope.ROLE, "role")
    with pytest.raises(SessionConfigurationError, match="not durable"):
        harness.sessions.inspect(key, "resume-1")
    assert harness.boundary.calls == 1


class _SchemaFailureStore(FakeAgentInvocationStore):
    def __init__(self, operation: str) -> None:
        super().__init__()
        self.operation = operation

    def load_optional(self) -> AgentInvocationState | None:
        if self.operation == "load":
            return AgentInvocationState.model_validate({"schema_version": 2})
        return super().load_optional()

    def save(self, model: AgentInvocationState) -> None:
        if self.operation == "save":
            AgentInvocationState.model_validate({"schema_version": 2})
        super().save(model)


@pytest.mark.parametrize("operation", ["load", "save"])
def test_schema_failure_at_store_boundary_is_typed_and_prevents_dispatch(
    harness: _Harness, operation: str
) -> None:
    store = _SchemaFailureStore(operation)
    sessions = ClientAgentSessions(harness.client, store)
    sessions.bind(KEY, harness.spec, harness.turn)
    with pytest.raises(SessionPersistenceError):
        sessions.resume(KEY, harness.message, "resume-1")
    assert harness.boundary.calls == 1


@pytest.mark.parametrize("implementation", ["fake", "client"])
def test_initial_reply_replays_after_all_services_reconstructed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, implementation: str
) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "state"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _namespace(workspace)
    calls: list[AgentTurnRequest] = []
    spec = AgentSessionSpec(
        role="implementer",
        provider="codex",
        workspace=workspace,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    turn = AgentTurnRequest(message="first", invocation_id="initial-1")
    factory = FakeAgentSessions if implementation == "fake" else ClientAgentSessions

    def reconstruct() -> tuple[AgentClient, ClientAgentSessions]:
        reopened = _namespace_existing(workspace)
        checkpoints = DurableSessionStore(reopened.slot("sessions.json", AgentSessionState))
        client = AgentClient(
            FakeDriver(answer="waiting", on_turn=calls.append),
            provider="codex",
            session_store=checkpoints,
        )
        return client, factory(client, reopened.slot("invocations.json", AgentInvocationState))

    client, sessions = reconstruct()
    try:
        result = sessions.start(KEY, spec, turn)
        assert isinstance(result, Completed)
    finally:
        client.close()
    client, recovered = reconstruct()
    try:
        assert recovered.inspect(KEY, "initial-1") == result
        assert recovered.start(KEY, spec, turn) == result
        with pytest.raises(InvocationConflictError, match="payload changed"):
            recovered.start(KEY, spec, replace(turn, message="changed"))
        assert len(calls) == 1
    finally:
        client.close()


def _namespace_existing(root: Path) -> StateNamespace:
    return Project.open(root).state.local_namespace("run-1", "agent")


def test_explicit_drained_interruption_releases_key_without_replaying_unknown(
    harness: _Harness,
) -> None:
    def fail() -> None:
        raise KeyboardInterrupt

    harness.boundary.effect = fail
    with pytest.raises(KeyboardInterrupt):
        harness.sessions.resume(KEY, harness.message, "interrupted-1")
    interrupted = harness.sessions.inspect(KEY, "interrupted-1")
    assert isinstance(interrupted, Unknown)
    with pytest.raises(SessionResumeError, match="KeyboardInterrupt"):
        harness.sessions.resume(KEY, harness.message, "next-1")
    harness.sessions.release_interrupted(KEY, "interrupted-1")
    harness.boundary.effect = None
    assert isinstance(harness.reconstruct().resume(KEY, harness.message, "next-1"), Completed)
    assert harness.sessions.resume(KEY, harness.message, "interrupted-1") == interrupted


def test_initial_schema_rejection_without_checkpoint_fences_live_and_recovered_corrections(
    tmp_path: Path,
) -> None:
    calls: list[AgentTurnRequest] = []

    def execute(turn: AgentTurnRequest) -> None:
        calls.append(turn)
        if len(calls) == 1:
            detail = "value must be an integer"
            raise AgentOutputSchemaError(detail)

    client = AgentClient(FakeDriver(answer="done", on_turn=execute))
    ledger = FakeAgentInvocationStore()
    sessions = ClientAgentSessions(client, ledger)
    spec = AgentSessionSpec(
        role="worker",
        provider="fake",
        workspace=tmp_path,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    initial = AgentTurnRequest(message="work", invocation_id="initial")
    try:
        outcome = sessions.start(KEY, spec, initial)
        assert isinstance(outcome, InvalidResponse)
        assert "integer" in outcome.detail
        recovered = ClientAgentSessions(client, ledger)
        with pytest.raises(SessionResumeError, match="acknowledged provider checkpoint is missing"):
            recovered.start(KEY, spec, replace(initial, invocation_id="initial/correction"))
        with pytest.raises(SessionResumeError, match="acknowledged provider checkpoint is missing"):
            sessions.start(KEY, spec, replace(initial, invocation_id="initial/correction"))
        assert len(calls) == 1
    finally:
        client.close()


def test_drained_predispatch_interruption_needs_no_journal_entry(harness: _Harness) -> None:
    harness.sessions.release_interrupted(KEY, "not-dispatched")
    assert isinstance(harness.sessions.inspect(KEY, "not-dispatched"), Unknown)
    assert isinstance(harness.sessions.resume(KEY, harness.message, "next"), Completed)
    other = AgentSessionKey(SessionScope.HYPOTHESIS, "other")
    with pytest.raises(InvocationConflictError, match="another key"):
        harness.sessions.release_interrupted(other, "next")


@pytest.mark.parametrize("generation", [2, 3, 17])
def test_new_generation_preserves_the_previous_unknown_fence(
    harness: _Harness, generation: int
) -> None:
    """A new key permits new work without retiring the ambiguous conversation."""

    def fail() -> None:
        detail = "lost acknowledgement"
        raise OSError(detail)

    harness.boundary.effect = fail
    unknown = harness.sessions.resume(KEY, harness.message, "resume-unknown")
    assert isinstance(unknown, Unknown)
    assert unknown.checkpoint is not None
    harness.boundary.effect = None
    key = AgentSessionKey.for_member("implementer", "H-01", generation=generation)
    initial = replace(
        harness.turn, expected_provider_session_id=None, invocation_id="new-generation"
    )
    recovered = harness.reconstruct()
    completed = recovered.start(key, harness.spec, initial)
    assert isinstance(completed, Completed)
    assert completed.session_key != unknown.session_key
    before = harness.boundary.calls
    recovered = harness.reconstruct()
    assert recovered.start(key, harness.spec, initial) == completed
    assert recovered.resume(KEY, harness.message, "resume-unknown") == unknown
    with pytest.raises(SessionResumeError, match="lost acknowledgement") as failure:
        recovered.start(KEY, harness.spec, replace(initial, invocation_id="unsafe-old-generation"))
    assert failure.value.detail == unknown.detail
    assert harness.boundary.calls == before


@given(
    role=st.text(min_size=1, max_size=32),
    member=st.text(min_size=1, max_size=32),
    generation=st.integers(min_value=1, max_value=2**31),
)
def test_generation_identity_is_roundtrippable_and_disjoint(
    role: str, member: str, generation: int
) -> None:
    key = AgentSessionKey.for_member(role, member, generation=generation)
    assert AgentSessionKey.parse(str(key)) == key
    assert key.durable
    assert key != AgentSessionKey.for_member(role, f"{member}:{generation}")
    assert key != AgentSessionKey.for_member(role, member, generation=generation + 1)
    assert key != AgentSessionKey.for_member(role + ":", member, generation=generation)
    assert key != AgentSessionKey.for_member(role, member + ":", generation=generation)
