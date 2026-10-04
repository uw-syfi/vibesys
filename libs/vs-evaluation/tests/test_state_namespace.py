"""The in-memory evaluation namespace shares Project's typed storage contract."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel, ConfigDict

from vs_evaluation.api.testing import InMemoryEvaluationNamespace
from vs_project.api import (
    OrchestrationDescriptor,
    Project,
    ProjectStateError,
    RunEnvironmentRecord,
    RunExecutionRecord,
    StateModelNotFoundError,
)

if TYPE_CHECKING:
    from pathlib import Path

    from _pytest.fixtures import FixtureRequest

    from vs_evaluation.api import EvaluationStateNamespace


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: list[int]


class WrongRecord(BaseModel):
    values: str


@pytest.fixture(params=("memory", "project"))
def namespace(
    request: FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> EvaluationStateNamespace:
    if request.param == "memory":
        return InMemoryEvaluationNamespace()
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path.parent / "state-home"))
    project = Project.open(tmp_path)
    project.state.create_project("contract")
    manifest = project.state.new_run_manifest(
        "Namespace contract",
        run_id="namespace-contract",
        trusted_input_baseline="a" * 40,
        branch="test/namespace",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=RunExecutionRecord(
            model="test",
            agent_backend="fake",
            compute_backend="cpu",
            requested_profiler="none",
            resolved_profiler="none",
            agent_roles={},
        ),
        orchestration=OrchestrationDescriptor(id="test", config_version=1, options={}),
    )
    project.state.create_run(manifest)
    return project.state.portable_namespace(manifest.run_id, "namespace-contract")


def test_replacement_reads_are_strict_detached_and_missing_is_explicit(
    namespace: EvaluationStateNamespace,
) -> None:
    assert namespace.load_optional("record.json", Record) is None
    with pytest.raises(StateModelNotFoundError):
        namespace.load("record.json", Record)
    original = Record(values=[1])
    namespace.save("record.json", original)
    original.values.append(2)
    loaded = namespace.load("record.json", Record)
    assert loaded.values == [1]
    loaded.values.append(3)
    assert namespace.load("record.json", Record).values == [1]
    namespace.save("record.json", Record(values=[]))
    assert namespace.load("record.json", Record).values == []
    namespace.save("record.json", WrongRecord(values="invalid"))
    with pytest.raises(ProjectStateError, match="values"):
        namespace.load_optional("record.json", Record)
    assert namespace.delete("record.json")
    assert not namespace.delete("record.json")


@pytest.mark.parametrize(
    "path", ["", "../escape.json", "a//b.json", "a\\b.json", "/absolute.json", "."]
)
def test_unsafe_paths_are_rejected(namespace: EvaluationStateNamespace, path: str) -> None:
    with pytest.raises(ProjectStateError, match="path"):
        namespace.save(path, Record(values=[]))
