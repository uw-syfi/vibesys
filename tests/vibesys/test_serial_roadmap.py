"""A serial run's roadmap owner keeps the designer's items in the run's state, where they stay.

`roadmap_entries` builds the owner of the roadmap operations over the run's portable state,
opened through `Project`. These tests drive it over a real project on disk: a put is read back,
a reopened project (a resumed run, a new process) still holds it, a batch the roadmap refuses is
a typed rejection that changes nothing, `observe` says a put landed only once it has, and a
corrupt document is a typed failure rather than an exception.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from vibesys.orchestration.single.launch import SingleLaunch
from vibesys.serial_roadmap import ROADMAP_NAMESPACE, RoadmapOwner, roadmap_entries
from vs_github.api import FakeGitHubClient
from vs_issue_tracker.api import (
    ROADMAP_DOCUMENT,
    PutRoadmapItems,
    ReadRoadmap,
    RoadmapItem,
    RoadmapItemKind,
    RoadmapItemsPut,
    RoadmapSnapshot,
    RoadmapStatus,
)
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord
from vs_project.api.testing import run_execution_record
from vs_runtime.api import OwnedOperationResources
from vs_runtime.api.core import Applied, NotApplied
from vs_sim.api.testing import ManualClock, SimThreads

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

RUN = "run-1"
_MAJOR = RoadmapItem(
    id="major-1", kind=RoadmapItemKind.MAJOR, title="fuse", status=RoadmapStatus.TODO
)
_MINOR = RoadmapItem(
    id="minor-1",
    kind=RoadmapItemKind.MINOR,
    title="build breaks",
    status=RoadmapStatus.IN_PROGRESS,
    major="major-1",
)


def _project(root: Path) -> Project:
    root.mkdir(parents=True, exist_ok=True)
    project = Project.open(root)
    project.state.create_project("test", now=datetime(2026, 1, 1, tzinfo=UTC))
    run = project.state.new_run_manifest(
        "Run 1",
        run_id=RUN,
        trusted_input_baseline="a" * 40,
        branch=f"vibesys/{RUN}",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="single-agent", config_version=1, options={}),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    project.state.create_run(run)
    return project


def _owner(project: Project) -> RoadmapOwner:
    """The owner the production wiring builds for the run, over its portable state."""
    entries = roadmap_entries(
        OwnedOperationResources(
            run_id=RUN,
            project=project,
            options=SingleLaunch(interface="service", max_rounds=1),
            variables={},
            templates=project.root,
            clock=ManualClock(start=0.0),
            threads=SimThreads(),
            github=FakeGitHubClient(),
        )
    )
    assert {entry.registration.descriptor.kind for entry in entries} == {
        ReadRoadmap().kind,
        PutRoadmapItems(items=(_MAJOR,)).kind,
    }
    owners = {id(entry.owner): entry.owner for entry in entries}
    assert len(owners) == 1
    (owner,) = owners.values()
    assert isinstance(owner, RoadmapOwner)
    return owner


def _items(outcome: BaseModel) -> tuple[RoadmapItem, ...]:
    assert isinstance(outcome, RoadmapSnapshot)
    assert outcome.status == "succeeded", outcome.detail
    return outcome.items


def test_a_put_is_read_back_and_kept_for_a_reopened_project(tmp_path: Path) -> None:
    owner = _owner(_project(tmp_path))

    assert _items(owner.perform(ReadRoadmap())) == ()
    assert owner.perform(PutRoadmapItems(items=(_MAJOR, _MINOR))) == RoadmapItemsPut(
        status="succeeded"
    )

    assert _items(_owner(Project.open(tmp_path)).perform(ReadRoadmap())) == (_MAJOR, _MINOR)


def test_a_refused_batch_is_a_typed_rejection_that_changes_nothing(tmp_path: Path) -> None:
    owner = _owner(_project(tmp_path))
    owner.perform(PutRoadmapItems(items=(_MAJOR,)))
    orphan = _MINOR.model_copy(update={"major": "major-9"})

    outcome = owner.perform(PutRoadmapItems(items=(orphan,)))

    assert isinstance(outcome, RoadmapItemsPut)
    assert outcome.status == "rejected"
    assert "major-9" in outcome.detail
    assert _items(owner.perform(ReadRoadmap())) == (_MAJOR,)


def test_observe_says_a_put_landed_only_once_the_roadmap_holds_it(tmp_path: Path) -> None:
    owner = _owner(_project(tmp_path))
    put = PutRoadmapItems(items=(_MAJOR, _MINOR))

    assert isinstance(owner.observe(put), NotApplied)
    owner.perform(PutRoadmapItems(items=(_MAJOR,)))
    assert isinstance(owner.observe(put), NotApplied)
    owner.perform(put)
    assert owner.observe(put) == Applied(RoadmapItemsPut(status="succeeded"))


def test_a_corrupt_roadmap_document_is_a_typed_failure(tmp_path: Path) -> None:
    project = _project(tmp_path)
    namespace = project.state.portable_namespace(RUN, ROADMAP_NAMESPACE)
    namespace.write_bytes(ROADMAP_DOCUMENT, b"not json")
    owner = _owner(project)

    read = owner.perform(ReadRoadmap())
    put = owner.perform(PutRoadmapItems(items=(_MAJOR,)))

    assert isinstance(read, RoadmapSnapshot)
    assert read.status == "failed"
    assert ROADMAP_DOCUMENT in read.detail
    assert isinstance(put, RoadmapItemsPut)
    assert put.status == "failed"
    assert isinstance(owner.observe(PutRoadmapItems(items=(_MAJOR,))), NotApplied)
