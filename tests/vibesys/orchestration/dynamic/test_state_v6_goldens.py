"""Version-6 goldens stay lossless through the production strict state store."""

import json
from pathlib import Path

import pytest
from tests.support.run_execution import run_execution_record

from vibesys.orchestration.dynamic import DynamicState
from vs_project.api import (
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
    StateNamespace,
)


def _namespace(tmp_path: Path) -> StateNamespace:
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


@pytest.mark.parametrize("fixture", ["empty", "completed"])
def test_version_6_golden_migrates_without_changing_planner_data(
    tmp_path: Path, fixture: str
) -> None:
    encoded = (Path(__file__).parent / "fixtures" / "state_v6" / f"{fixture}.json").read_bytes()
    namespace = _namespace(tmp_path)
    (namespace.external_directory() / "state.json").write_bytes(encoded)

    migrated = namespace.load("state.json", DynamicState)

    assert migrated.schema_version == DynamicState().schema_version
    assert getattr(migrated, "agent", None) is None
    old_data = json.loads(encoded)
    new_data = migrated.model_dump(mode="json")
    assert new_data.pop("lifecycle") == {"intents": {}, "continuations": {}, "stopped": False}
    new_data.pop("agent", None)
    for item in new_data["workstreams"]:
        assert item.pop("invocation_sequence") == 0
    new_data["schema_version"] = 6
    assert new_data == old_data
    if fixture == "completed":
        assert migrated.input_measurement is not None
        assert migrated.input_measurement.attempts == 2
        assert migrated.workstreams[0].plan.evidence

    namespace.save("state.json", migrated)
    assert namespace.load("state.json", DynamicState) == migrated
