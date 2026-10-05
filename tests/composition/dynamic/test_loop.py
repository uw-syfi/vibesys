"""Whole dynamic runs on the core path, over the production host and scripted agents."""

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

from vibesys.orchestration.dynamic.agents import IMPLEMENTER

if TYPE_CHECKING:
    from pathlib import Path


def test_a_hypothesis_is_adopted_and_the_next_one_builds_on_it(tmp_path: Path) -> None:
    loop_input = LoopInput.create(tmp_path)
    seen: dict[str, int] = {}

    def build_on_first(agent: Turn) -> dict[str, object]:
        seen["second-start"] = agent.value()
        agent.set_value(3)
        return implemented("H2")

    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("H1")), portfolio(workstream("H2", parent_hypothesis_id="H1")))
        .implement("H1", edit_to(2, "H1"))
        .judge("H1", PASS)
        .implement("H2", build_on_first)
        .judge("H2", PASS)
    )

    run = run_request(loop_input.request(max_rounds=2), agents)

    assert run.error is None
    assert run.succeeded is True
    assert agents.unscripted == []
    records = CoreRecords(loop_input, run.run_id)
    assert records.outcome == ("terminal", "success")
    first, second = records.strategy["hypotheses"]
    assert [item["hypothesis_id"] for item in (first, second)] == ["H1", "H2"]
    (first_round,) = first["rounds"]
    (second_round,) = second["rounds"]
    assert records.baseline_metric() == 1.0
    assert first_round["metrics"][0]["value"] == 2.0
    assert second_round["metrics"][0]["value"] == 3.0
    # The second hypothesis branches from the first's trusted candidate.
    assert records.attempt("H2")["parent"] == first_round["candidate"]
    assert seen["second-start"] == 2
    # The search keeps the best trusted candidate.
    selection = records.selection
    assert selection is not None
    assert selection["kind"] == "retained_candidate"
    assert selection["revision"] == second_round["candidate"]
    assert len(agents.prompts(IMPLEMENTER.id)) == 2
    assert loop_input.sbatch_count() == 3
    # Adoption applies the winner to the input project.
    assert (loop_input.root / "queue.py").read_text(encoding="utf-8") == "VALUE = 3\n"
