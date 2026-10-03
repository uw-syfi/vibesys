"""The dynamic state survives the project's real, strict state store."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from tests.support.run_execution import run_execution_record
from tests.vibesys.orchestration.dynamic._support import (
    INPUT_BASELINE,
    Script,
    baseline_run,
    dynamic_options,
    implementation,
    portfolio,
    throughput,
)

from vibesys.orchestration.dynamic import (
    PLUGIN,
    DynamicState,
    EvidenceReference,
    ImplementerResult,
)
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vs_project.api import (
    OrchestrationDescriptor,
    Project,
    ProjectStateError,
    RunEnvironmentRecord,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project.api import StateNamespace


def _namespace(tmp_path: Path) -> StateNamespace:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "OBJECTIVE.md").write_text("Make it fast.\n", encoding="utf-8")
    project = Project.open(tmp_path)
    project.state.create_project("test")
    run = project.state.new_run_manifest(
        "Run 1",
        run_id="run-1",
        trusted_input_baseline="a" * 40,
        branch="vibesys/run-1",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="dynamic", config_version=1, options={}),
    )
    project.state.create_run(run)
    return project.state.local_namespace("run-1", "dynamic")


def _finished_state(tmp_path: Path) -> DynamicState:
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("kept")],
            IMPLEMENTER.id: [implementation("kept")],
            JUDGE.id: [{"passed": True, "analysis": "Candidate is correct."}],
        }
    )

    async def scenario() -> DynamicState | None:
        run = baseline_run(tmp_path, script)
        run.evaluation.script_benchmark(INPUT_BASELINE, throughput(2.0))
        await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1))
        return await run.state.load(DynamicState)

    state = asyncio.run(scenario())
    assert state is not None
    plan = state.workstreams[0].plan
    plan.evidence = (EvidenceReference(location="profile/trace.json", purpose="hot kernel"),)
    return state


def test_state_with_tuple_fields_round_trips_through_the_project_state_store(
    tmp_path: Path,
) -> None:
    """The store loads with ``strict=True``, which rejects JSON arrays for tuple
    fields when a ``mode="before"`` validator forces Python-mode validation.
    """
    state = _finished_state(tmp_path / "scenario")
    namespace = _namespace(tmp_path / "project")

    namespace.save("state.json", state)

    assert namespace.load("state.json", DynamicState) == state
    assert namespace.load("state.json", DynamicState).workstreams[0].plan.evidence


def test_empty_state_round_trips_through_the_project_state_store(tmp_path: Path) -> None:
    namespace = _namespace(tmp_path)

    namespace.save("state.json", DynamicState())

    assert namespace.load("state.json", DynamicState) == DynamicState()


def test_state_written_by_an_older_version_loads_through_the_project_state_store(
    tmp_path: Path,
) -> None:
    current = _finished_state(tmp_path / "scenario")
    legacy = current.model_dump(mode="json")
    legacy["schema_version"] = 3
    for item in legacy["workstreams"]:
        budget = item.pop("budget")
        item["attempts"], item["refunded_attempts"] = budget["spent"], budget["refunded"]
    namespace = _namespace(tmp_path / "project")
    (namespace.external_directory() / "state.json").write_text(json.dumps(legacy), encoding="utf-8")

    assert namespace.load("state.json", DynamicState) == current


def test_version_5_state_with_a_validation_recipe_artifact_loads_without_it(
    tmp_path: Path,
) -> None:
    current = _finished_state(tmp_path / "scenario")
    legacy = current.model_dump(mode="json")
    legacy["schema_version"] = 5
    assert legacy["workstreams"][0]["implementation"] is not None
    legacy["workstreams"][0]["implementation"]["validation_recipe_artifact"] = "validation/r.json"
    namespace = _namespace(tmp_path / "project")
    (namespace.external_directory() / "state.json").write_text(json.dumps(legacy), encoding="utf-8")

    loaded = namespace.load("state.json", DynamicState)

    assert loaded == current
    assert loaded.schema_version == 6
    implementation_json = loaded.workstreams[0].implementation.model_dump(mode="json")
    assert "validation_recipe_artifact" not in implementation_json


def test_current_state_with_a_validation_recipe_artifact_is_rejected(tmp_path: Path) -> None:
    current = _finished_state(tmp_path / "scenario")
    stale = current.model_dump(mode="json")
    stale["workstreams"][0]["implementation"]["validation_recipe_artifact"] = "validation/r.json"
    namespace = _namespace(tmp_path / "project")
    (namespace.external_directory() / "state.json").write_text(json.dumps(stale), encoding="utf-8")

    with pytest.raises(ProjectStateError, match="validation_recipe_artifact"):
        namespace.load("state.json", DynamicState)


def test_implementer_result_schema_has_no_validation_recipe_field() -> None:
    schema = ImplementerResult.model_json_schema()

    assert "validation_recipe_artifact" not in schema["properties"]
    with pytest.raises(ValueError, match="validation_recipe_artifact"):
        ImplementerResult.model_validate(
            {**implementation("x"), "validation_recipe_artifact": "validation/r.json"}
        )
