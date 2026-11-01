"""A project reads the latest record of a run's local state store, and creates no state to ask."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_project.api import Committed, Project, StoredEnvelope

if TYPE_CHECKING:
    from pathlib import Path

_ENVELOPE = StoredEnvelope(revision=0, schema_version=1, payload=b"{}")


def _commit(project: Project, run_id: str) -> None:
    store = project.state_store(run_id)
    fence = store.acquire("host", 0.0, 10.0)
    assert fence is not None
    assert isinstance(store.commit(None, _ENVELOPE, fence, 1.0), Committed)


def test_a_run_without_a_record_reads_as_none_and_creates_no_state(tmp_path: Path) -> None:
    project = Project.open(tmp_path)

    assert project.stored_record("run-1") is None
    assert not (tmp_path / ".vibesys").exists()


def test_a_committed_record_is_read_back_by_a_project_opened_again(tmp_path: Path) -> None:
    _commit(Project.open(tmp_path), "run-1")

    assert Project.open(tmp_path).stored_record("run-1") == _ENVELOPE
