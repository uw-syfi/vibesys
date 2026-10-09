"""Journaled provider turns preserve the conversation needed by their consumer."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from agentshim.testing import FakeExecutor, TokenUsage, scripted_resume_failure, scripted_turn
from hypothesis import example, given
from hypothesis import strategies as st

from vs_agent.api import (
    AgentClient,
    AgentExecutionPolicy,
    AgentOutputSchemaError,
    AgentSessionKey,
    AgentSessionSpec,
    AgentTurnRequest,
    ClientAgentSessions,
    Completed,
    InvalidResponse,
    SessionDisposition,
    SessionResumeError,
    SessionScope,
    Unknown,
)
from vs_agent.api.testing import (
    FakeAgentInvocationStore,
    FakeDriver,
    FakeTurnScript,
    fake_agentshim_driver,
)
from vs_prompts.api import TemplateRenderer


def _spec(workspace: Path, role: str = "implementer") -> AgentSessionSpec:
    return AgentSessionSpec(
        role=role,
        provider="codex",
        workspace=workspace,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )


@given(
    preceding=st.integers(min_value=0, max_value=4),
    corrected=st.booleans(),
    heavy=st.booleans(),
    role=st.sampled_from(("implementer", "judge", "profiler")),
)
@example(preceding=0, corrected=False, heavy=True, role="implementer")
@example(preceding=0, corrected=True, heavy=False, role="implementer")
@example(preceding=1, corrected=False, heavy=False, role="implementer")
@example(preceding=1, corrected=True, heavy=True, role="implementer")
def test_journaled_turn_sequence_preserves_resume_proof(
    *, preceding: int, corrected: bool, heavy: bool, role: str
) -> None:
    with TemporaryDirectory() as directory:
        workspace = Path(directory)
        (workspace / "resume.j2").write_text("{{ evidence }}", encoding="utf-8")
        message = TemplateRenderer(workspace).render_template("resume.j2", evidence="settled")
        executor = FakeExecutor(
            scripted_turn(
                "codex",
                text="waiting",
                session_id="thread-1",
                usage=TokenUsage(input_tokens=20_000_000 if heavy else 1),
            )
        )
        driver = fake_agentshim_driver(provider="codex", executor=executor)
        key = AgentSessionKey(SessionScope.MEMBER, f"{role}:worker")
        ledger = FakeAgentInvocationStore()
        with AgentClient(driver, provider="codex") as client:
            sessions = ClientAgentSessions(client, ledger)
            spec = _spec(workspace, role)
            sessions.bind(key, spec, AgentTurnRequest(message="", label="resume"))
            for number in range(preceding + 1):
                identity = f"invocation-{number + 1}"
                outcome = sessions.start(
                    key, spec, AgentTurnRequest(message="work", invocation_id=identity)
                )
                assert isinstance(outcome, Completed), outcome
            if corrected:
                outcome = sessions.start(
                    key,
                    spec,
                    AgentTurnRequest(
                        message="correct wait", invocation_id=f"{identity}/wait-correction"
                    ),
                )
                assert isinstance(outcome, Completed), outcome
            checkpoint = sessions.checkpoint(key)
            resumed = sessions.resume(key, message, f"{identity}/resume")
            assert isinstance(resumed, Completed), resumed
            assert resumed.checkpoint == checkpoint
            assert resumed.result.provider_session_id == "thread-1"
            accepted = len(executor.requests)
            assert sessions.resume(key, message, f"{identity}/resume") == resumed
            assert len(executor.requests) == accepted


@pytest.mark.parametrize("provider", ["claude", "codex", "gemini", "opencode"])
def test_journaled_later_start_never_replays_after_provider_refuses_resume(
    tmp_path: Path, provider: str
) -> None:
    executor = FakeExecutor(
        [
            scripted_turn(provider, text="waiting", session_id="thread-1"),
            scripted_resume_failure(provider, session_id="thread-1"),
            scripted_turn(provider, text="must not replay", session_id="thread-2"),
        ]
    )
    driver = fake_agentshim_driver(provider=provider, executor=executor)
    key = AgentSessionKey(SessionScope.MEMBER, "implementer:worker")
    spec = AgentSessionSpec(
        role="implementer",
        provider=provider,
        workspace=tmp_path,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    ledger = FakeAgentInvocationStore()
    with AgentClient(driver, provider=provider) as client:
        sessions = ClientAgentSessions(client, ledger)
        first = sessions.start(key, spec, AgentTurnRequest(message="work", invocation_id="first"))
        assert isinstance(first, Completed)
        failed = sessions.start(key, spec, AgentTurnRequest(message="more", invocation_id="later"))
        assert isinstance(failed, Unknown)
        assert len(executor.requests) == 2
        assert "thread-1" in executor.requests[1].argv
        assert (
            sessions.start(key, spec, AgentTurnRequest(message="more", invocation_id="later"))
            == failed
        )
        assert len(executor.requests) == 2


def test_journaled_initial_turn_without_provider_identity_stays_unresolved(tmp_path: Path) -> None:
    executor = FakeExecutor(scripted_turn("codex", text="waiting"))
    driver = fake_agentshim_driver(provider="codex", executor=executor)
    key = AgentSessionKey(SessionScope.MEMBER, "implementer:worker")
    spec = _spec(tmp_path)
    turn = AgentTurnRequest(message="work", invocation_id="first")
    ledger = FakeAgentInvocationStore()
    with AgentClient(driver, provider="codex") as client:
        sessions = ClientAgentSessions(client, ledger)
        outcome = sessions.start(key, spec, turn)
        assert isinstance(outcome, Unknown)
        assert sessions.start(key, spec, turn) == outcome
        assert len(executor.requests) == 1


def test_journaled_later_start_refuses_replaced_provider_identity(tmp_path: Path) -> None:
    executor = FakeExecutor(
        [
            scripted_turn("codex", text="waiting", session_id="thread-1"),
            scripted_turn("codex", text="unexpected replacement", session_id="thread-2"),
        ]
    )
    driver = fake_agentshim_driver(provider="codex", executor=executor)
    key = AgentSessionKey(SessionScope.MEMBER, "implementer:worker")
    spec = _spec(tmp_path)
    turn = AgentTurnRequest(message="more", invocation_id="later")
    ledger = FakeAgentInvocationStore()
    with AgentClient(driver, provider="codex") as client:
        sessions = ClientAgentSessions(client, ledger)
        first = sessions.start(key, spec, AgentTurnRequest(message="work", invocation_id="first"))
        assert isinstance(first, Completed)
        outcome = sessions.start(key, spec, turn)
        assert isinstance(outcome, Unknown)
        assert "provider reset or replaced" in outcome.detail
        assert sessions.start(key, spec, turn) == outcome
        assert len(executor.requests) == 2


@pytest.mark.parametrize("implementation", ["fake", "agentshim"])
@pytest.mark.parametrize("required", [False, True])
def test_session_renewal_threshold_survives_a_strict_turn(
    tmp_path: Path, implementation: str, *, required: bool
) -> None:
    driver = (
        FakeDriver(script=FakeTurnScript(answers=("done",), reset_after_turn=2))
        if implementation == "fake"
        else fake_agentshim_driver(
            provider="codex",
            executor=FakeExecutor(scripted_turn("codex", text="done", session_id="thread-1")),
        )
    )
    session = driver.create_session(_spec(tmp_path))
    try:
        first = session.run_turn(AgentTurnRequest("first", require_provider_checkpoint=True))
        strict = session.run_turn(
            AgentTurnRequest("second", expected_provider_session_id=first.provider_session_id)
        )
        assert strict.disposition is SessionDisposition.REUSABLE
        last = session.run_turn(AgentTurnRequest("third", require_provider_checkpoint=required))
        assert last.disposition is (
            SessionDisposition.REUSABLE if required else SessionDisposition.RESET_REQUIRED
        )
    finally:
        session.close()
        driver.close()


@pytest.mark.parametrize("schema_rejected", [False, True])
def test_later_start_refuses_lost_checkpoint_after_reconstruction(
    tmp_path: Path, *, schema_rejected: bool
) -> None:
    calls: list[AgentTurnRequest] = []
    key = AgentSessionKey(SessionScope.MEMBER, "implementer:worker")
    spec = _spec(tmp_path)
    ledger = FakeAgentInvocationStore()
    first_driver = FakeDriver(
        script=FakeTurnScript(answers=("waiting", AgentOutputSchemaError("invalid reply"))),
        on_turn=calls.append,
    )
    with AgentClient(first_driver, provider="codex") as client:
        first = ClientAgentSessions(client, ledger).start(
            key, spec, AgentTurnRequest(message="work", invocation_id="first")
        )
        assert isinstance(first, Completed)
        if schema_rejected:
            first = ClientAgentSessions(client, ledger).start(
                key, spec, AgentTurnRequest(message="more", invocation_id="invalid")
            )
            assert isinstance(first, InvalidResponse)
        assert first.checkpoint is not None
    # The inert default checkpoint store deliberately models lost persisted
    # provider state after the journal has acknowledged accepted work.
    with AgentClient(
        FakeDriver(answer="must not replay", on_turn=calls.append), provider="codex"
    ) as client:
        recovered = ClientAgentSessions(client, ledger)
        with pytest.raises(SessionResumeError, match="acknowledged provider checkpoint is missing"):
            recovered.start(key, spec, AgentTurnRequest(message="more", invocation_id="later"))
        assert len(calls) == 1 + int(schema_rejected)
        assert isinstance(recovered.inspect(key, "later"), Unknown)


def test_later_start_refuses_changed_configuration_before_dispatch(tmp_path: Path) -> None:
    calls: list[AgentTurnRequest] = []
    key = AgentSessionKey(SessionScope.MEMBER, "implementer:worker")
    spec = _spec(tmp_path)
    ledger = FakeAgentInvocationStore()
    with AgentClient(
        FakeDriver(answer="waiting", on_turn=calls.append), provider="codex"
    ) as client:
        sessions = ClientAgentSessions(client, ledger)
        first = sessions.start(key, spec, AgentTurnRequest(message="work", invocation_id="first"))
        assert isinstance(first, Completed)
        changed = AgentSessionSpec(
            role=spec.role,
            provider=spec.provider,
            workspace=spec.workspace,
            policy=spec.policy,
            model="changed",
        )
        outcome = sessions.start(
            key, changed, AgentTurnRequest(message="more", invocation_id="later")
        )
        assert isinstance(outcome, Unknown)
        assert "session specification changed" in outcome.detail
        assert len(calls) == 1
