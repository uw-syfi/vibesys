"""Accepted evaluation yields retain their provider conversation for resume."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, override

import pytest
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
    SessionResumeError,
)
from vs_agent.api.testing import HandSession

if TYPE_CHECKING:
    from pathlib import Path

    from vs_agent.api import (
        AgentCapabilities,
        AgentObserver,
        AgentTurnRequest,
        AgentTurnResult,
        SessionStore,
        SkillSelection,
    )
    from vs_agent.api.testing import FakeAgentClient


class BudgetedSession(HandSession):
    """A scripted provider that retires unbound turns at a budget boundary."""

    def __init__(self, *, spec: AgentSessionSpec, scenario: BudgetedLauncher) -> None:
        super().__init__(spec)
        self.scenario = scenario
        self.adopted_id: str | None = None

    @property
    def key(self) -> AgentSessionKey:
        return AgentSessionKey.for_member(self.spec.role, str(self.spec.workspace))

    @override
    def run_turn(
        self, request: AgentTurnRequest, observer: AgentObserver | None = None
    ) -> AgentTurnResult:
        if self.closed:
            raise SessionResumeError(str(self.key), "provider session is closed")
        if (
            request.expected_provider_session_id is not None
            and self.adopted_id != request.expected_provider_session_id
        ):
            raise SessionResumeError(str(self.key), "expected provider history was not adopted")
        result = self.scenario.engine.run(
            session_spec=self.spec, turn=request, session_key=self.key, observer=observer
        )
        assert result.provider_session_id is not None
        self.scenario.histories.add(result.provider_session_id)
        self.scenario.records.append((self.spec.role, request, result))
        identity = result.provider_session_id
        self.adopted_id = identity
        self.scenario.counts[identity] = self.scenario.counts.get(identity, 0) + 1
        if (
            self.spec.role == IMPLEMENTER.id
            and self.scenario.counts[identity] >= self.scenario.retire_after
            and request.expected_provider_session_id is None
            and not request.require_provider_checkpoint
        ):
            self.scenario.engine.evict_session(self.key)
            self.scenario.histories.remove(result.provider_session_id)
            self.adopted_id = None
            return replace(result, restarted=True)
        return result

    @override
    def adopt(self, session_id: str) -> bool:
        if (
            self.adopted_id is not None
            or session_id not in self.scenario.histories
            or self.scenario.engine.provider_session_id(self.key) != session_id
        ):
            return False
        self.adopted_id = session_id
        return True

    @override
    def cancel(self) -> None:
        self.scenario.engine.cancel()

    @override
    def close(self) -> None:
        self.closed = True


@dataclass
class BudgetedLauncher:
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

    def launch(self, spec: AgentSessionSpec) -> BudgetedSession:
        if self.closed:
            raise SessionResumeError(spec.role, "session launcher is closed")
        session = BudgetedSession(spec=spec, scenario=self)
        self.sessions.append(session)
        return session

    def close(self) -> None:
        # Each scoped AgentClient owns its launcher and session views. The
        # scripted provider's history belongs to the composed scenario and
        # persists after a view closes, as a CLI provider's rollout does.
        self.closed = True
        for session in self.sessions:
            session.close()


@dataclass
class BudgetedAgents:
    scripts: ScriptedAgents
    retire_after: int
    launcher: BudgetedLauncher | None = None
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
        self.launcher = BudgetedLauncher(
            self.engine, self.retire_after, self.histories, self.records, self.counts
        )
        return AgentClient(self.launcher, provider="codex", session_store=session_store)


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
    assert agents.launcher is not None
    records = [
        (request, result) for role, request, result in agents.records if role == IMPLEMENTER.id
    ]
    waited, resumed = records[-2:]
    assert resumed[0].expected_provider_session_id == waited[1].provider_session_id
    assert resumed[1].provider_session_id == waited[1].provider_session_id
    assert item.evaluation is not None
    assert item.evaluation.metric_value == 2.0


# The retirement boundary falls after one more turn per rejected candidate and per
# corrected wait, so the cases span no rejection, a rejection, and a correction.
@pytest.mark.parametrize(
    ("failed_attempts", "correct_wait"),
    [(0, False), (1, False), (1, True)],
    ids=["unrejected", "plain", "corrected"],
)
def test_valid_wait_resumes_after_provider_budget_boundary(
    tmp_path: Path, *, failed_attempts: int, correct_wait: bool
) -> None:
    """r24b provider waits could lose their checkpoint before host acceptance."""
    _run_wait_sequence(tmp_path, failed_attempts=failed_attempts, correct_wait=correct_wait)


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
    assert agents.launcher is not None
    records = [
        (request, result) for role, request, result in agents.records if role == IMPLEMENTER.id
    ]
    identities = {result.provider_session_id for _, result in records}
    assert len(identities) == 1
    for request, result in records:
        if request.expected_provider_session_id is not None:
            assert request.expected_provider_session_id == result.provider_session_id
    assert len(records) == 4
