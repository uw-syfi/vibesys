"""The planner prompt tells the truth about where a new workstream starts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.composition.dynamic._harness import (
    PASS,
    CoreRecords,
    LoopInput,
    ScriptedAgents,
    Turn,
    edit_to,
    implemented,
    portfolio,
    run_request,
    workstream,
)

from vibesys.orchestration.dynamic.agents import ORCHESTRATOR

if TYPE_CHECKING:
    from pathlib import Path


def test_a_workstream_with_no_parent_starts_from_the_input_and_the_prompt_says_so(
    tmp_path: Path,
) -> None:
    loop_input = LoopInput.create(tmp_path)
    seen: dict[str, int] = {}

    def start_from_default(agent: Turn) -> dict[str, object]:
        seen["start"] = agent.value()
        agent.set_value(3)
        return implemented("H2")

    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("H1")), portfolio(workstream("H2")))
        .implement("H1", edit_to(2, "H1"))
        .judge("H1", PASS)
        .implement("H2", start_from_default)
        .judge("H2", PASS)
    )

    run = run_request(loop_input.request(max_rounds=2), agents)

    assert run.error is None
    assert agents.unscripted == []
    # The input has VALUE = 1 and H1 retained VALUE = 2: with no parent named, H2 starts
    # from the input, not from the best trusted candidate.
    assert seen["start"] == 1
    records = CoreRecords(loop_input, run.run_id)
    (first_round,) = records.strategy["hypotheses"][0]["rounds"]
    second_prompt = agents.prompts(ORCHESTRATOR.id)[1]
    assert "root or the best trusted candidate" not in second_prompt
    assert "a new workstream that names no parent starts from it" in second_prompt
    # The best trusted candidate is reachable only as an explicitly named parent.
    assert "Buildable candidates" in second_prompt
    assert records.attempt("H2")["parent"] != first_round["candidate"]
