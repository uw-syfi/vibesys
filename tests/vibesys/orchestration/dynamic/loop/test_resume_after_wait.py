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
    implemented,
    load_state,
    options,
    portfolio,
    run_loop,
    workstream,
)

from vibesys.orchestration.dynamic.agents import IMPLEMENTER
from vibesys.orchestration.dynamic.models import WorkstreamPhase
from vs_agent.api import AgentClient, AgentSessionKey, AgentSessionSpec, SessionResumeError

if TYPE_CHECKING:
    from vs_agent.api import AgentCapabilities, AgentObserver, AgentTurnRequest, AgentTurnResult
    from vs_agent.api.testing import FakeAgentClient


@dataclass
class BudgetedSession:
    """A scripted provider with the production discretionary retirement policy."""

    engine: FakeAgentClient
    spec: AgentSessionSpec
    retire_after: int
    histories: set[str]
    records: list[tuple[str, AgentTurnRequest, AgentTurnResult]]
    turns: int = 0
    closed: bool = False

    @property
    def key(self) -> AgentSessionKey:
        return AgentSessionKey.for_member(self.spec.role, str(self.spec.workspace))

    def run_turn(
        self, request: AgentTurnRequest, observer: AgentObserver | None = None
    ) -> AgentTurnResult:
        if self.closed:
            raise SessionResumeError(str(self.key), "provider session is closed")
        result = self.engine.run(
            session_spec=self.spec, turn=request, session_key=self.key, observer=observer
        )
        assert result.provider_session_id is not None
        self.histories.add(result.provider_session_id)
        self.records.append((self.spec.role, request, result))
        self.turns += 1
        # The compatibility read lets the exact same regression execute on the
        # pre-fix request contract, which did not express checkpoint retention.
        checkpoint_required = getattr(request, "require_provider_checkpoint", False)
        if (
            self.spec.role == IMPLEMENTER.id
            and self.turns == self.retire_after
            and request.expected_provider_session_id is None
            and not checkpoint_required
        ):
            self.engine.evict_session(self.key)
            self.histories.remove(result.provider_session_id)
            return replace(result, disposition=type(result.disposition).RESET_REQUIRED)
        return result

    def resume_provider_session(self, session_id: str) -> bool:
        if (
            session_id not in self.histories
            or self.engine.provider_session_id(self.key) is not None
        ):
            return False
        self.engine.set_session(self.key, provider_session_id=session_id)
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

    @property
    def capabilities(self) -> AgentCapabilities:
        return self.engine.capabilities

    def create_session(self, spec: AgentSessionSpec) -> BudgetedSession:
        return BudgetedSession(self.engine, spec, self.retire_after, self.histories, self.records)

    def close(self) -> None:
        self.engine.close()


@dataclass
class BudgetedAgents:
    scripts: ScriptedAgents
    retire_after: int
    driver: BudgetedDriver | None = None

    def client(self) -> AgentClient:
        self.driver = BudgetedDriver(self.scripts.client(), self.retire_after)
        return AgentClient(self.driver, provider="codex")


def _run_wait_sequence(base: Path, *, failed_attempts: int, correct_wait: bool) -> None:
    loop_input = LoopInput.create(base)
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
    # A completed blocked reply is a definite attempt failure, so retrying the
    # member is permitted and keeps its provider conversation.
    scripts.implement("H1", *[implemented("H1", outcome="blocked")] * failed_attempts)
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
    )

    assert run.error is None, run.error
    state = load_state(loop_input, run.run_id)
    (item,) = state.workstreams
    assert item.phase is WorkstreamPhase.EVALUATED, item.last_error
    assert run.succeeded is True
    assert scripts.unscripted == []
    assert len(handle) == 1
    assert agents.driver is not None
    records = [
        (request, result)
        for role, request, result in agents.driver.records
        if role == IMPLEMENTER.id
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
    loop_input = LoopInput.create(tmp_path)
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
    agents = BudgetedAgents(scripts, retire_after=3)

    run = run_loop(loop_input, agents, options())

    assert run.error is None, run.error
    (item,) = load_state(loop_input, run.run_id).workstreams
    assert item.phase is WorkstreamPhase.EVALUATED, item.last_error
    assert run.succeeded is True
    assert scripts.unscripted == []
    assert len(handles) == 2
    assert agents.driver is not None
    records = [
        (request, result)
        for role, request, result in agents.driver.records
        if role == IMPLEMENTER.id
    ]
    identities = {result.provider_session_id for _, result in records}
    assert len(identities) == 1
    for request, result in records:
        if request.expected_provider_session_id is not None:
            assert request.expected_provider_session_id == result.provider_session_id
    assert len(records) == 4


@settings(max_examples=6)
@example(failed_attempts=0, correct_wait=False)
@example(failed_attempts=0, correct_wait=True)
@example(failed_attempts=1, correct_wait=False)
@example(failed_attempts=1, correct_wait=True)
@given(failed_attempts=st.integers(min_value=0, max_value=2), correct_wait=st.booleans())
def test_wait_resume_keeps_identity_across_invocation_sequences(
    *, failed_attempts: int, correct_wait: bool
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        _run_wait_sequence(
            Path(directory), failed_attempts=failed_attempts, correct_wait=correct_wait
        )
