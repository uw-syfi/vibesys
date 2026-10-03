"""Whole dynamic-plugin runs over the production layers with scripted agents."""

from __future__ import annotations

from typing import TYPE_CHECKING

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
from vibesys.orchestration.dynamic.models import WorkstreamPhase

if TYPE_CHECKING:
    from pathlib import Path


def test_a_hypothesis_is_adopted_and_the_next_one_builds_on_it(tmp_path: Path) -> None:
    loop_input = LoopInput.create(tmp_path)
    seen: dict[str, int] = {}

    def build_on_first(agent: Turn) -> dict[str, object]:
        seen["second-start"] = agent.value()
        agent.set_value(3)
        agent.evaluate("accuracy")
        return implemented("H2")

    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("H1")), portfolio(workstream("H2")))
        .implement("H1", edit_to(2, "H1", ("accuracy", "benchmark")))
        .judge("H1", PASS)
        .implement("H2", build_on_first)
        .judge("H2", PASS)
    )

    run = run_loop(loop_input, agents, options(max_rounds=2))

    assert run.error is None
    assert run.succeeded is True
    assert agents.unscripted == []
    state = load_state(loop_input, run.run_id)
    first, second = state.workstreams
    assert [item.phase for item in state.workstreams] == [WorkstreamPhase.EVALUATED] * 2
    assert state.baseline is not None
    assert state.baseline.metric_value == 1.0
    assert first.evaluation is not None
    assert first.evaluation.metric_value == 2.0
    # The second hypothesis branches from the first's trusted candidate.
    assert second.parent_revision == first.candidate_revision
    assert seen["second-start"] == 2
    assert second.evaluation is not None
    assert second.evaluation.metric_value == 3.0
    assert state.winner_revision == second.candidate_revision
    assert not state.adoption_pending
    assert (loop_input.root / "queue.py").read_text(encoding="utf-8") == "VALUE = 3\n"
    assert len(agents.prompts(IMPLEMENTER.id)) == 2
