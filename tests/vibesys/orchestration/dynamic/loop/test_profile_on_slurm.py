"""A planned profile on the Slurm run environment produces trusted evidence.

r16 profiled 12 times and every profile ended unsupported: no evaluation
executor produced the profile evidence kind, and the profiler's own capture
could not upload its job script through the run's Slurm broker. These scenarios
run the production loop over the Fake cluster, whose GPU node fakes only the
profiler binary, so the capture, staging, and evidence path is production code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.agents import ORCHESTRATOR, PROFILER

from ._harness import (
    PASS,
    LoopInput,
    ScriptedAgents,
    Turn,
    edit_to,
    load_state,
    options,
    planner_history,
    portfolio,
    profile_workstream,
    run_loop,
    workstream,
)

if TYPE_CHECKING:
    from pathlib import Path


def _trusted_profile(agent: Turn) -> dict[str, object]:
    """Profile through the framework's evaluation tool and cite its evidence."""
    reply = agent.evaluate("profile")
    result = reply["result"]
    assert isinstance(result, dict), reply
    assert result["outcome"] == "completed", reply
    stages = result["stages"]
    assert isinstance(stages, list)
    (stage,) = stages
    evidence = stage["result"]
    assert evidence["kind"] == "profile", evidence
    assert evidence["outcome"] == "passed", evidence
    assert "queue_step holds 75%" in evidence["semantic_summary"]
    evidence_ids = [evidence["evidence_id"]]
    return {
        "outcome": "observed",
        "narrative": "queue_step holds 75% of device time.",
        "evidence_ids": evidence_ids,
        "attribution": [{"name": "queue_step", "cost": 3.0, "share": 0.75}],
    }


def test_a_profile_on_slurm_produces_trusted_evidence_that_reaches_the_next_plan(
    tmp_path: Path,
) -> None:
    loop_input = LoopInput.create(tmp_path, profiled=True)
    agents = (
        ScriptedAgents()
        .plan(
            portfolio(workstream("A")),
            portfolio(profile_workstream("prof-A", "A", "Where does A spend its time?")),
            portfolio(workstream("B")),
        )
        .implement("A", edit_to(2, "A"))
        .judge("A", PASS)
        .profile(_trusted_profile)
        .implement("B", edit_to(3, "B"))
        .judge("B", PASS)
    )

    run = run_loop(loop_input, agents, options(max_rounds=3))

    assert run.error is None
    assert agents.unscripted == []
    state = load_state(loop_input, run.run_id)
    (profile,) = state.profiles
    assert profile.outcome is not None
    assert profile.outcome.status.value == "observed"
    assert profile.outcome.evidence_ids
    row = planner_history(agents.prompts(ORCHESTRATOR.id)[2])["prof-A"]
    assert row["status"] == "observed"
    assert row["evidence_ids"] == list(profile.outcome.evidence_ids)
    assert "Where does A spend its time?" in agents.prompts(PROFILER.id)[0]
