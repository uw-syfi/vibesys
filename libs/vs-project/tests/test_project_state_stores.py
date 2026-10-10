"""A Project opens each run's store through its injected factory, or the local default."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_project.api import (
    CommitFault,
    Committed,
    FakeStateStores,
    Project,
    StateStoreFactory,
    StoredEnvelope,
)

if TYPE_CHECKING:
    from pathlib import Path

_RUN_IDS = st.from_regex(r"[a-z][a-z0-9-]{0,15}", fullmatch=True)
_ENVELOPE = StoredEnvelope(revision=0, schema_version=1, payload=b"{}")


def _local(_root: Path) -> StateStoreFactory | None:
    return None


def _fake(_root: Path) -> StateStoreFactory | None:
    return FakeStateStores()


@pytest.fixture(params=[_local, _fake], ids=["local", "fake"])
def stores(request: pytest.FixtureRequest, tmp_path: Path) -> StateStoreFactory | None:
    return request.param(tmp_path)


def _commit(project: Project, run_id: str) -> None:
    store = project.state_store(run_id)
    fence = store.acquire("host", 0.0, 10.0)
    assert fence is not None
    assert isinstance(store.commit(None, _ENVELOPE, fence, 1.0), Committed)


def test_a_run_without_a_record_reads_as_none_and_creates_no_state(
    tmp_path: Path, stores: StateStoreFactory | None
) -> None:
    project = Project.open(tmp_path, state_stores=stores)

    assert project.stored_record("run-1") is None
    assert not (tmp_path / ".vibesys").exists()


def test_a_committed_record_is_read_back_by_a_project_opened_again(
    tmp_path: Path, stores: StateStoreFactory | None
) -> None:
    _commit(Project.open(tmp_path, state_stores=stores), "run-1")

    assert Project.open(tmp_path, state_stores=stores).stored_record("run-1") == _ENVELOPE


@settings(max_examples=25, deadline=None)
@given(first=_RUN_IDS, second=_RUN_IDS)
def test_runs_of_one_project_never_share_a_store(first: str, second: str) -> None:
    stores = FakeStateStores()
    project = Project.open("/", state_stores=stores)

    same = project.state_store(first) is project.state_store(second)

    assert same == (first == second)


def test_fault_plans_are_rejected_for_injected_stores(tmp_path: Path) -> None:
    project = Project.open(tmp_path, state_stores=FakeStateStores())

    with pytest.raises(ValueError, match="fault plans"):
        project.state_store("run-1", fault_plan=[CommitFault.FAILED])
