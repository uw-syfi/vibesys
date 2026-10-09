"""The in-memory host namespace shares Project's public model-store contract."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict
from tests.support.run_execution import run_execution_record

from vs_project.api import (
    FakeStateModels,
    OrchestrationDescriptor,
    Project,
    ProjectStateError,
    RunEnvironmentRecord,
    StateDocumentDamagedError,
    StateNamespace,
)

if TYPE_CHECKING:
    from pathlib import Path


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int
    values: list[int]


@pytest.fixture(params=["project", "fake"])
def namespace(request: pytest.FixtureRequest, tmp_path: Path) -> StateNamespace | FakeStateModels:
    """Provision either implementation through its public owning API."""
    if request.param == "fake":
        return FakeStateModels()
    project = Project.open(tmp_path)
    now = datetime(2026, 8, 11, tzinfo=UTC)
    project.state.create_project("Queue", now=now)
    manifest = project.state.new_run_manifest(
        "Queue",
        branch="vibesys/queue",
        vibesys_version="0.2.0",
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="team-search", config_version=1, options={}),
        trusted_input_baseline="a" * 40,
        now=now,
        unique=UUID(int=1),
    )
    project.state.create_run(manifest)
    return project.state.local_namespace(manifest.run_id, "attempts")


def test_models_are_optional_detached_and_replace_atomically(
    namespace: StateNamespace | FakeStateModels,
) -> None:
    assert namespace.load_optional("nested/cursor.json", _Record) is None
    record = _Record(count=1, values=[2])
    namespace.save("nested/cursor.json", record)
    record.values.append(3)
    loaded = namespace.load_optional("nested/cursor.json", _Record)
    assert loaded == _Record(count=1, values=[2])
    assert loaded is not None
    loaded.values.append(4)
    assert namespace.load_optional("nested/cursor.json", _Record) == _Record(count=1, values=[2])
    namespace.save("nested/cursor.json", _Record(count=5, values=[]))
    assert namespace.load_optional("nested/cursor.json", _Record) == _Record(count=5, values=[])


@pytest.mark.parametrize(
    "contents",
    [
        b'{"count":"leaked-value","values":[]}',
        b'{"unexpected":"leaked-value"}',
        b"leaked-garbage",
        b'{"count":1,"values":[]',
        b"\xff leaked-bytes",
    ],
)
def test_persisted_models_validate_strictly_without_echoing_inputs(
    namespace: StateNamespace | FakeStateModels, contents: bytes
) -> None:
    namespace.write_bytes("cursor.json", contents)
    with pytest.raises(StateDocumentDamagedError, match="Invalid VibeSys state model") as failure:
        namespace.load_optional("cursor.json", _Record)
    assert "leaked" not in str(failure.value)


@pytest.mark.parametrize("name", ["a\0b.json", "x" * 256, "dir/" + "y" * 256])
def test_names_no_filesystem_accepts_are_rejected_as_unsafe_paths(
    namespace: StateNamespace | FakeStateModels, name: str
) -> None:
    with pytest.raises(ProjectStateError):
        namespace.write_bytes(name, b"{}")
    with pytest.raises(ProjectStateError):
        namespace.read_bytes(name)


def test_a_file_and_a_directory_cannot_share_a_name(
    namespace: StateNamespace | FakeStateModels,
) -> None:
    namespace.write_bytes("leaf", b"{}")
    namespace.write_bytes("branch/leaf", b"{}")
    with pytest.raises(ProjectStateError):
        namespace.write_bytes("leaf/child", b"{}")
    with pytest.raises(ProjectStateError):
        namespace.write_bytes("branch", b"{}")
    with pytest.raises(ProjectStateError):
        namespace.read_bytes("branch")
    with pytest.raises(ProjectStateError):
        namespace.load_optional("branch", _Record)
    assert namespace.read_bytes("leaf") == b"{}"


@pytest.mark.parametrize("path", ["../escape.json", "/absolute.json", "", "safe/../../escape"])
def test_paths_reject_unsafe_routes(namespace: StateNamespace | FakeStateModels, path: str) -> None:
    with pytest.raises(ProjectStateError):
        namespace.save(path, _Record(count=1, values=[]))
    with pytest.raises(ProjectStateError):
        namespace.load_optional(path, _Record)
