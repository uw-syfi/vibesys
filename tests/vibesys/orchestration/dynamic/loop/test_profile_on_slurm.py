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
    implemented,
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


def _submit_profile(agent: Turn) -> dict[str, object]:
    """Yield the profiler's semantic capture to the host without an agent await."""
    return {"kind": "waiting_for_evaluation", "handles": [agent.submit("profile")]}


def _trusted_profile(agent: Turn) -> dict[str, object]:
    """Read the host-settled profile evidence in the resumed conversation."""
    records = agent.accepted_evidence("profile")
    (evidence,) = [record for record in records if record["evidence_id"] in agent.prompt]
    assert evidence["kind"] == "profile", evidence
    assert evidence["outcome"] == "passed", evidence
    assert "queue_step holds 75%" in evidence["semantic_summary"]
    assert evidence["evidence_id"] in agent.prompt
    return {
        "outcome": "observed",
        "narrative": "queue_step holds 75% of device time.",
        "evidence_ids": [evidence["evidence_id"]],
        "attribution": [{"name": "queue_step", "cost": 3.0, "share": 0.75}],
    }


def test_a_profile_on_slurm_produces_trusted_evidence_that_reaches_the_next_plan(
    tmp_path: Path,
) -> None:
    loop_input = LoopInput.create(tmp_path, profiled=True)

    def below_gate(agent: Turn) -> dict[str, object]:
        (agent.workspace / "queue.py").write_text("VALUE = 2\nREQUIRED = 100\n", encoding="utf-8")
        return implemented("A")

    agents = (
        ScriptedAgents()
        .plan(
            portfolio(workstream("A")),
            portfolio(profile_workstream("prof-A", "A", "Where does A spend its time?")),
            portfolio(workstream("B")),
        )
        .implement("A", below_gate)
        .judge("A", PASS)
        .profile(_submit_profile, _trusted_profile)
        .implement("B", edit_to(3, "B"))
        .judge("B", PASS)
    )

    run = run_loop(loop_input, agents, options(max_rounds=3, max_retries_per_round=1))

    assert run.error is None
    assert agents.unscripted == []
    state = load_state(loop_input, run.run_id)
    candidate = state.workstreams[0]
    assert candidate.evaluation is not None
    assert candidate.evaluation.accuracy_passed
    assert not candidate.evaluation.benchmark_passed
    assert candidate.evaluation.partial_measurement is not None
    assert candidate.evaluation.partial_measurement.value == 2
    (profile,) = state.profiles
    assert profile.outcome is not None
    assert profile.outcome.status.value == "observed", profile.outcome.failure
    assert profile.outcome.evidence_ids
    row = planner_history(agents.prompts(ORCHESTRATOR.id)[2])["prof-A"]
    assert row["status"] == "observed"
    assert row["evidence_ids"] == list(profile.outcome.evidence_ids)
    profiler_prompts = agents.prompts(PROFILER.id)
    assert len(profiler_prompts) == 2
    assert "Where does A spend its time?" in profiler_prompts[0]
    assert "Every evaluation you" in profiler_prompts[1]


def test_a_profile_whose_workload_cannot_run_is_unsupported_without_a_profiler_turn(
    tmp_path: Path,
) -> None:
    """r18: a load-failed capture was passed evidence, and two profiler turns argued over it."""
    loop_input = LoopInput.create(tmp_path, profiled=True)
    loop_input.fail_profile_workloads()
    agents = (
        ScriptedAgents()
        .plan(
            portfolio(profile_workstream("prof-root", None, "Where does the root spend time?")),
            portfolio(workstream("A")),
            portfolio(workstream("B")),
        )
        .implement("A", edit_to(2, "A"))
        .judge("A", PASS)
        .implement("B", edit_to(3, "B"))
        .judge("B", PASS)
    )

    # The unsupported profile is refunded, so two rounds hold both workstreams.
    run = run_loop(loop_input, agents, options(max_rounds=2))

    assert run.error is None
    assert agents.unscripted == []
    assert agents.prompts(PROFILER.id) == []
    state = load_state(loop_input, run.run_id)
    (profile,) = state.profiles
    assert profile.outcome is not None
    assert profile.outcome.status.value == "unsupported"
    assert profile.outcome.diagnosis is not None
    assert "not profilable: the configured workload did not run" in profile.outcome.diagnosis
    assert len(profile.outcome.evidence_ids) == 1


def test_a_run_without_a_profiler_fails_when_its_only_plan_is_profiles(tmp_path: Path) -> None:
    """r17: a plan dropped whole after correction ended the run as a completed search."""
    loop_input = LoopInput.create(tmp_path)
    # Each planning turn is corrected once; a turn still invalid is a turn
    # fault, retried up to max_retries_per_round (2) turns before the run fails.
    agents = ScriptedAgents().plan(
        *(portfolio(profile_workstream(f"prof-{n}", None)) for n in range(1, 5))
    )

    run = run_loop(loop_input, agents, options(max_rounds=2))

    assert run.succeeded is None
    assert run.error is not None
    assert "the planner scheduled no valid workstream after correction" in str(run.error)
    assert "workstreams[0].kind" in str(run.error)
    assert agents.unscripted == []
