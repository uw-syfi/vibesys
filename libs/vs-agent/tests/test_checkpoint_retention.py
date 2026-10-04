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
    AgentSessionKey,
    AgentSessionSpec,
    AgentTurnRequest,
    ClientAgentSessions,
    Completed,
    SessionScope,
    Unknown,
)
from vs_agent.api.testing import FakeAgentInvocationStore, fake_agentshim_driver
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
