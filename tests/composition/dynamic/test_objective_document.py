"""The run's committed objective names the skills where the agents find them.

The operator writes the objective against the skills' source paths (``resources/skills/...``);
the agents see those skills under ``.agents/skills/``. The run commits an effective objective
that spells the paths the agents can open, to the workstream's isolated workspace and to the
framework's input-baseline benchmark alike, and leaves the operator's own file untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.composition.dynamic._harness import (
    PASS,
    CoreRecords,
    LoopInput,
    ScriptedAgents,
    implemented,
    portfolio,
    run_request,
    workstream,
)

from vibesys.api import RunStatus
from vibesys.dynamic_roles import IMPLEMENTER

if TYPE_CHECKING:
    from pathlib import Path

    from tests.composition.dynamic._harness import Turn


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
    objective = "Raise queue throughput. Follow resources/skills/objective-policy/floor.md.\n"
    (loop_input.root / "OBJECTIVE.md").write_text(objective, encoding="utf-8")
    seen: dict[str, bool] = {}

    def edit_and_read_the_skill(agent: Turn) -> dict[str, object]:
        seen["floor"] = (agent.workspace / ".agents/skills/objective-policy/floor.md").is_file()
        agent.set_value(2)
        return implemented("objective-regression")

    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("objective-regression")))
        .implement("objective-regression", edit_and_read_the_skill)
        .judge("objective-regression", PASS)
    )

    run = run_request(
        loop_input.request(skills_dir=str(skill)),
        agents,
        slurm_process=loop_input.connector,
        state_stores=loop_input.state_stores,
    )

    assert run.error is None, run.error
    assert (run.succeeded, run.status) == (True, RunStatus.COMPLETED)
    assert run.notes() == []
    assert agents.unscripted == []
    assert seen == {"floor": True}
    assert len(agents.invocations(IMPLEMENTER.id, "objective-regression")) == 1
    records = CoreRecords(loop_input, run.run_id)
    assert records.baseline_metric() == 1.0
    (round_,) = records.strategy["hypotheses"][0]["rounds"]
    assert round_["metrics"][0]["value"] == 2.0
    runtime = loop_input.project().state.portable_namespace(run.run_id, "runtime")
    document = runtime.external_directory() / "effective-objective.md"
    assert document.read_text(encoding="utf-8") == objective.replace(
        "resources/skills/", ".agents/skills/"
    )
    assert (loop_input.root / "OBJECTIVE.md").read_text(encoding="utf-8") == objective
    # One framework baseline plus the candidate's accuracy and benchmark jobs.
    assert loop_input.sbatch_count() == 2
