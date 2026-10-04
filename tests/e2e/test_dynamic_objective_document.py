"""Default-CI dynamic start over real wiring, scripted agents and Fake Slurm.

Unlike the opt-in provider smoke, this needs no agent CLI or cluster access.
It checks the run's committed objective across the isolated workspace boundary
for both a workstream and the framework's input-baseline benchmark.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from tests.vibesys.orchestration.dynamic.loop._harness import (
    PASS,
    LoopInput,
    ScriptedAgents,
    edit_to,
    load_state,
    options,
    portfolio,
    run_loop,
    workstream,
)

from vs_project.api import Project

if TYPE_CHECKING:
    from pathlib import Path


def test_dynamic_workstream_and_framework_baseline_verify_committed_objective(
    tmp_path: Path,
) -> None:
    loop_input = LoopInput.create(tmp_path)
    skill = tmp_path / "resources" / "skills" / "objective-policy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: objective-policy\ndescription: Preserve accuracy.\n---\n# Objective policy\n",
        encoding="utf-8",
    )
    (skill / "floor.md").write_text("Preserve accuracy.\n", encoding="utf-8")
    (loop_input.root / "OBJECTIVE.md").write_text(
        "Raise queue throughput. Follow resources/skills/objective-policy/floor.md.\n",
        encoding="utf-8",
    )
    loop_input = replace(loop_input, skills_dirs=(skill,))
    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("objective-regression")))
        .implement("objective-regression", edit_to(2, "objective-regression"))
        .judge("objective-regression", PASS)
    )

    run = run_loop(loop_input, agents, options())

    assert run.error is None, run.error
    assert run.succeeded is True
    assert run.notes() == []
    assert agents.unscripted == []
    state = load_state(loop_input, run.run_id)
    (member,) = state.workstreams
    assert member.evaluation is not None, member.last_error
    assert state.baseline is not None
    assert state.baseline.benchmark_passed
    assert state.baseline.metric_value == 1.0
    assert member.evaluation.accuracy_passed
    assert member.evaluation.benchmark_passed
    assert member.evaluation.metric_value == 2.0
    runtime = Project.open(loop_input.root).state.portable_namespace(run.run_id, "runtime")
    document = runtime.external_directory() / "effective-objective.md"
    assert document.read_text(encoding="utf-8") == (
        "Raise queue throughput. Follow .agents/skills/objective-policy/floor.md.\n"
    )
    # One framework baseline plus the candidate's accuracy and benchmark jobs.
    assert sum("sbatch " in command for command in loop_input.cluster_commands()) == 3
