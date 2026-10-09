"""Accepted evaluation yields retain their provider conversation for resume."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic.loop._harness import (
    PASS,
    LoopInput,
    ScriptedAgents,
    Turn,
    edit_to,
    implemented,
    load_state,
    options,
    portfolio,
    run_loop,
    workstream,
)

from vibesys.orchestration.dynamic.agents import IMPLEMENTER
from vs_agent.api import (
    NULL_SKILL_SELECTION,
    AgentClient,
    AgentSessionKey,
    AgentSessionSpec,
    SessionDisposition,
    SessionResumeError,
)

if TYPE_CHECKING:
    from vs_agent.api import (
        AgentCapabilities,
        AgentObserver,
        AgentTurnRequest,
        AgentTurnResult,
        SessionStore,
        SkillSelection,
    )
    from vs_agent.api.testing import FakeAgentClient


@dataclass
class BudgetedSession:
    """A scripted provider that retires unbound turns at a budget boundary."""

    engine: FakeAgentClient
    spec: AgentSessionSpec
    retire_after: int
    histories: set[str]
    records: list[tuple[str, AgentTurnRequest, AgentTurnResult]]
    counts: dict[str, int]
    adopted: str | None = None
    closed: bool = False

    @property
    def key(self) -> AgentSessionKey:
        return AgentSessionKey.for_member(self.spec.role, str(self.spec.workspace))

    def run_turn(
        self, request: AgentTurnRequest, observer: AgentObserver | None = None
    ) -> AgentTurnResult:
        if self.closed:
            raise SessionResumeError(str(self.key), "provider session is closed")
        if (
            request.expected_provider_session_id is not None
            and self.adopted != request.expected_provider_session_id
        ):
            raise SessionResumeError(str(self.key), "expected provider history was not adopted")
        result = self.engine.run(
            session_spec=self.spec, turn=request, session_key=self.key, observer=observer
        )
        assert result.provider_session_id is not None
        self.histories.add(result.provider_session_id)
        self.records.append((self.spec.role, request, result))
        identity = result.provider_session_id
        self.adopted = identity
        self.counts[identity] = self.counts.get(identity, 0) + 1
        # The compatibility read lets the exact same regression execute on the
        # pre-fix request contract, which did not express checkpoint retention.
        checkpoint_required = getattr(request, "require_provider_checkpoint", False)
        if (
            self.spec.role == IMPLEMENTER.id
            and self.counts[identity] >= self.retire_after
            and request.expected_provider_session_id is None
            and not checkpoint_required
        ):
            self.engine.evict_session(self.key)
            self.histories.remove(result.provider_session_id)
            self.adopted = None
            return replace(result, disposition=SessionDisposition.RESET_REQUIRED)
        return result

    def resume_provider_session(self, session_id: str) -> bool:
        if (
            self.adopted is not None
            or session_id not in self.histories
            or self.engine.provider_session_id(self.key) != session_id
        ):
            return False
        self.adopted = session_id
        return True

    def cancel(self) -> None:
        self.engine.cancel()

    def close(self) -> None:
        self.closed = True


@dataclass
class BudgetedDriver:
    """Keep real AgentClient checkpoint behavior above the scripted providers."""

    engine: FakeAgentClient
    retire_after: int
    histories: set[str] = field(default_factory=set)
    records: list[tuple[str, AgentTurnRequest, AgentTurnResult]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    sessions: list[BudgetedSession] = field(default_factory=list)
    closed: bool = False

    @property
    def capabilities(self) -> AgentCapabilities:
        return self.engine.capabilities

    def create_session(self, spec: AgentSessionSpec) -> BudgetedSession:
        if self.closed:
            raise SessionResumeError(spec.role, "provider driver is closed")
        session = BudgetedSession(
            self.engine, spec, self.retire_after, self.histories, self.records, self.counts
        )
        self.sessions.append(session)
        return session

    def close(self) -> None:
        # Each scoped AgentClient owns its driver and session views. The
        # scripted provider's history belongs to the composed scenario and
        # persists after a view closes, as a CLI provider's rollout does.
        self.closed = True
        for session in self.sessions:
            session.close()


@dataclass
class BudgetedAgents:
    scripts: ScriptedAgents
    retire_after: int
    driver: BudgetedDriver | None = None
    engine: FakeAgentClient | None = None
    histories: set[str] = field(default_factory=set)
    records: list[tuple[str, AgentTurnRequest, AgentTurnResult]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def client(
        self,
        *,
        session_store: SessionStore | None = None,
        skill_selection: SkillSelection = NULL_SKILL_SELECTION,
    ) -> AgentClient:
        return self.factory(session_store=session_store, skill_selection=skill_selection)

    def factory(
        self,
        *,
        session_store: SessionStore | None,
        skill_selection: SkillSelection = NULL_SKILL_SELECTION,
        **_kwargs: object,
    ) -> AgentClient:
        if self.engine is None:
            self.engine = self.scripts.client(skill_selection=skill_selection)
        self.driver = BudgetedDriver(
            self.engine, self.retire_after, self.histories, self.records, self.counts
        )
        return AgentClient(self.driver, provider="codex", session_store=session_store)


def _wait_input(base: Path) -> LoopInput:
    loop_input = LoopInput.create(base)
    declaration = loop_input.root / "vibesys.input.toml"
    declaration.write_text(
        declaration.read_text(encoding="utf-8").replace(
            "timeout_seconds = 30", "timeout_seconds = 120"
        ),
        encoding="utf-8",
    )
    return loop_input


def _run_wait_sequence(base: Path, *, failed_attempts: int, correct_wait: bool) -> None:
    loop_input = _wait_input(base)
    handle: list[str] = []

    def submit_wait(agent: Turn) -> dict[str, object]:
        agent.set_value(2)
        handle.append(agent.submit("accuracy"))
        if not correct_wait:
            agent.validate_wait(handle[0])
        return {
            "kind": "waiting_for_evaluation",
            "handles": [handle[0][:8] if correct_wait else handle[0]],
        }

    def corrected_wait(agent: Turn) -> dict[str, object]:
        assert "Correction required" in agent.prompt
        agent.validate_wait(handle[0])
        return {"kind": "waiting_for_evaluation", "handles": handle}

    def after_wait(agent: Turn) -> dict[str, object]:
        assert handle[0] in agent.prompt
        assert agent.accepted_evidence("accuracy")
        return implemented("H1")

    scripts = ScriptedAgents().plan(portfolio(workstream("H1")))
    # A rejected candidate is a definite attempt failure. The next attempt
    # continues the implementer's accepted conversation with review feedback.
    for attempt in range(failed_attempts):
        scripts.implement("H1", edit_to(3 + attempt, "H1"))
        scripts.judge("H1", {"passed": False, "analysis": "Unproven.", "feedback": "Prove it."})
    scripts.implement("H1", submit_wait)
    if correct_wait:
        scripts.implement("H1", corrected_wait)
    scripts.implement("H1", after_wait).judge("H1", PASS)
    retire_after = failed_attempts + 1 + int(correct_wait)

    agents = BudgetedAgents(scripts, retire_after)
    run = run_loop(
        loop_input,
        agents,
        options(max_retries_per_round=failed_attempts + 1),
        client_factory=agents.factory,
    )

    assert run.error is None, run.error
    state = load_state(loop_input, run.run_id)
    (item,) = state.workstreams
    assert state.winner_revision == item.candidate_revision, item.last_error
    assert run.succeeded is True
    assert scripts.unscripted == []
    assert len(handle) == 1
    assert agents.driver is not None
    records = [
        (request, result) for role, request, result in agents.records if role == IMPLEMENTER.id
    ]
    waited, resumed = records[-2:]
    assert resumed[0].expected_provider_session_id == waited[1].provider_session_id
    assert resumed[1].provider_session_id == waited[1].provider_session_id
    assert item.evaluation is not None
    assert item.evaluation.metric_value == 2.0


@pytest.mark.parametrize("correct_wait", [False, True], ids=["plain", "corrected"])
def test_valid_wait_resumes_after_provider_budget_boundary(
    tmp_path: Path, *, correct_wait: bool
) -> None:
    """r24b provider waits could lose their checkpoint before host acceptance."""
    _run_wait_sequence(tmp_path, failed_attempts=1, correct_wait=correct_wait)


def test_wait_correction_after_resumed_turn_keeps_checkpoint(tmp_path: Path) -> None:
    """r24b corrected a second wait after an earlier evaluation resume succeeded."""
    loop_input = _wait_input(tmp_path)
    handles: list[str] = []

    def first_wait(agent: Turn) -> dict[str, object]:
        agent.set_value(2)
        handles.append(agent.submit("accuracy"))
        agent.validate_wait(handles[-1])
        return {"kind": "waiting_for_evaluation", "handles": [handles[-1]]}

    def second_wait(agent: Turn) -> dict[str, object]:
        assert handles[0] in agent.prompt
        assert agent.accepted_evidence("accuracy")
        handles.append(agent.submit("benchmark"))
        return {"kind": "waiting_for_evaluation", "handles": [handles[-1][:8]]}

    def corrected_wait(agent: Turn) -> dict[str, object]:
        assert "validate_evaluation_wait" in agent.prompt
        agent.validate_wait(handles[-1])
        return {"kind": "waiting_for_evaluation", "handles": [handles[-1]]}

    def completed(agent: Turn) -> dict[str, object]:
        assert handles[-1] in agent.prompt
        assert agent.accepted_evidence("accuracy", "benchmark")
        return implemented("H1")

    scripts = (
        ScriptedAgents()
        .plan(portfolio(workstream("H1")))
        .implement("H1", first_wait, second_wait, corrected_wait, completed)
        .judge("H1", PASS)
    )
    agents = BudgetedAgents(scripts, retire_after=2)

    run = run_loop(loop_input, agents, options(), client_factory=agents.factory)

    assert run.error is None, run.error
    state = load_state(loop_input, run.run_id)
    (item,) = state.workstreams
    assert state.winner_revision == item.candidate_revision, item.last_error
    assert run.succeeded is True
    assert scripts.unscripted == []
    assert len(handles) == 2
    assert agents.driver is not None
    records = [
        (request, result) for role, request, result in agents.records if role == IMPLEMENTER.id
    ]
    identities = {result.provider_session_id for _, result in records}
    assert len(identities) == 1
    for request, result in records:
        if request.expected_provider_session_id is not None:
            assert request.expected_provider_session_id == result.provider_session_id
    assert len(records) == 4


@settings(max_examples=6)
@example(failed_attempts=0, correct_wait=False)
@given(failed_attempts=st.integers(min_value=0, max_value=2), correct_wait=st.booleans())
def test_wait_resume_keeps_identity_across_invocation_sequences(
    *, failed_attempts: int, correct_wait: bool
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        _run_wait_sequence(
            Path(directory), failed_attempts=failed_attempts, correct_wait=correct_wait
        )
