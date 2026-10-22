"""Every run starts from the project's own branch, whichever branch is checked out.

A run works on its own ``vibesys-runs/<run-id>`` branch, cut from the branch the user
had checked out when it started (its home branch). The run's branch stays checked out
after it ends; the next run checks the home branch out first, so it starts from the
user's work, not from the earlier run's branch, and the user's branch never carries
VibeSys commits. Every run stays listed and resumable through ``RunHistory`` whichever
branch is checked out, including the runs of projects whose run branches were stacked
before runs knew their home.

Each test is one world over the in-memory Git fake (``FakeGitRepositories``); every
process of that world (a run, the next run, a resume, a run list) opens its own
repository over the same disk.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_project.api import (
    GitRunHistory,
    GitTracker,
    NullGitTrackerEvents,
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
)
from vs_project.api.testing import FakeGitRepositories, run_execution_record
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project.api import GitRepository

_START = datetime(2026, 1, 1, tzinfo=UTC)

# Branch names a user may work on, nested hierarchies included.
_HOME_BRANCHES = st.from_regex(
    r"(main|dev|feature/[a-z]{1,8}|u/[a-z]{1,6}/[a-z0-9]{1,6})", fullmatch=True
)


class World:
    """One project directory and the in-memory Git disk every process of it shares."""

    def __init__(self, root: Path, home: str = "main") -> None:
        self.root = root
        self.disk = FakeGitRepositories()
        root.mkdir(parents=True)
        repository = self.git()
        repository.initialize(initial_branch=home)
        (root / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
        repository.stage_all(["main.py"])
        repository.commit("baseline")

    def git(self) -> GitRepository:
        return self.disk.repository(self.root)

    def tracker(self, run_id: str) -> GitTracker:
        return GitTracker(
            self.root, run_id=run_id, events=NullGitTrackerEvents(), repository=self.git()
        )

    def history(self) -> GitRunHistory:
        return GitRunHistory(Project.open(self.root), self.git())

    def listed(self) -> list[str]:
        return [manifest.run_id for manifest in self.history().recorded().runs]

    def checked_out(self) -> str | None:
        return self.git().current_branch()

    def tip(self, branch: str) -> str | None:
        return self.git().resolve_commit(branch)


def _record_run(root: Path, run_id: str, branch: str, index: int) -> Project:
    """Write a run's manifest as the product does, in the checkout (not committed)."""
    project = Project.open(root)
    now = _START + timedelta(minutes=index)
    project.state.create_project("Home branch test", now=now)
    project.state.create_run(
        project.state.new_run_manifest(
            "Home branch test",
            run_id=run_id,
            branch=branch,
            vibesys_version="test",
            trusted_input_baseline="0" * 40,
            run_environment=RunEnvironmentRecord(name="docker"),
            execution=run_execution_record(),
            orchestration=OrchestrationDescriptor(id="plain", config_version=1, options={}),
            now=now,
        )
    )
    return project


def _run(world: World, run_id: str, index: int) -> GitTracker:
    """Start a run as the product does and commit its initial state."""
    tracker = world.tracker(run_id)
    tracker.init(existing=False)
    project = _record_run(world.root, run_id, tracker.project_branch, index)
    tracker.snapshot_with_framework_metadata(
        f"vibesys: initialize run {run_id}", project.state.initialization_snapshot(run_id)
    )
    project.state.state_store_namespace(run_id).write_bytes("store.json", b"{}\n")
    return tracker


def _assert_branched_from(world: World, home_tip: str, run_ids: list[str], run_id: str) -> None:
    """The run's branch descends from the home tip and holds no other run's state."""
    git = world.git()
    branch = f"vibesys-runs/{run_id}"
    assert git.is_ancestor(home_tip, branch)
    state = Project.open(world.root).state
    for other in run_ids:
        if other == run_id:
            continue
        assert not git.is_ancestor(f"vibesys-runs/{other}", branch)
        manifest = (state.run_directory(other) / "run.json").as_posix()
        assert git.read_blob(branch, manifest) is None


@settings(max_examples=examples(pr=6, nightly=40), deadline=None)
@given(home=_HOME_BRANCHES, runs=st.integers(min_value=2, max_value=4), data=st.data())
def test_consecutive_runs_start_from_the_home_branch_and_all_stay_resumable(
    tmp_path_factory: pytest.TempPathFactory, home: str, runs: int, data: st.DataObject
) -> None:
    world = World(tmp_path_factory.mktemp("home") / "project", home)
    home_tip = world.tip(home)
    assert home_tip is not None
    run_ids = [f"run-{index}" for index in range(runs)]

    for index, run_id in enumerate(run_ids):
        tracker = _run(world, run_id, index)
        assert tracker.home_branch == home
        assert world.checked_out() == f"vibesys-runs/{run_id}"

    assert world.tip(home) == home_tip
    for run_id in run_ids:
        _assert_branched_from(world, home_tip, run_ids, run_id)
    assert world.listed() == run_ids

    before_resume = world.checked_out()
    for run_id in data.draw(st.permutations(run_ids)):
        resumed = world.tracker(run_id)
        resumed.init(existing=True)
        assert world.checked_out() == before_resume
        assert world.history().manifest(run_id).run_id == run_id
        assert world.listed() == run_ids

    tracker = _run(world, "after-resumes", runs)
    assert tracker.home_branch == home
    _assert_branched_from(world, home_tip, [*run_ids, "after-resumes"], "after-resumes")
    assert world.tip(home) == home_tip


def test_a_run_started_where_an_earlier_run_was_left_starts_from_its_home(
    tmp_path: Path,
) -> None:
    world = World(tmp_path / "project", "dev")
    home_tip = world.tip("dev")
    assert home_tip is not None
    _run(world, "killed", 0)
    assert world.checked_out() == "vibesys-runs/killed"

    tracker = _run(world, "next", 1)

    assert tracker.home_branch == "dev"
    assert world.checked_out() == "vibesys-runs/next"
    _assert_branched_from(world, home_tip, ["killed", "next"], "next")
    assert world.listed() == ["killed", "next"]


def _stacked_legacy_runs(world: World, run_ids: list[str]) -> None:
    """Run branches as VibeSys cut them before runs knew their home: each from the last."""
    git = world.git()
    for index, run_id in enumerate(run_ids):
        branch = f"vibesys-runs/{run_id}"
        git.switch_branch(branch, create=True)
        _record_run(world.root, run_id, branch, index)
        git.stage_all(["."], force=True)
        git.commit(f"vibesys: initialize run {run_id}")


def test_projects_with_stacked_run_branches_keep_every_run_and_start_from_home(
    tmp_path: Path,
) -> None:
    world = World(tmp_path / "project")
    home_tip = world.tip("main")
    assert home_tip is not None
    _stacked_legacy_runs(world, ["old-a", "old-b"])
    stacked = world.tip("vibesys-runs/old-b")

    tracker = _run(world, "new", 2)

    assert tracker.home_branch == "main"
    assert world.checked_out() == "vibesys-runs/new"
    _assert_branched_from(world, home_tip, ["old-a", "old-b", "new"], "new")
    assert world.tip("vibesys-runs/old-b") == stacked
    assert world.listed() == ["old-a", "old-b", "new"]
    before_resume = world.checked_out()
    for run_id in ("old-a", "old-b"):
        old = world.tracker(run_id)
        old.init(existing=True)
        assert world.checked_out() == before_resume
        assert world.history().manifest(run_id).run_id == run_id
        assert world.listed() == ["old-a", "old-b", "new"]


def test_a_new_run_refuses_a_run_branch_whose_home_is_unknown(tmp_path: Path) -> None:
    world = World(tmp_path / "project")
    git = world.git()
    git.switch_branch("dev", create=True)
    git.switch_branch("main")
    _stacked_legacy_runs(world, ["old"])

    with pytest.raises(ValueError, match="started from is unknown"):
        world.tracker("new").init(existing=False)
    assert world.checked_out() == "vibesys-runs/old"


@pytest.mark.regression
@example(home="main", contents="operator edits", staged=True)
@given(
    home=_HOME_BRANCHES,
    contents=st.text(alphabet="abcdef 0123", min_size=1, max_size=30),
    staged=st.booleans(),
)
def test_resume_preserves_the_operator_branch_tree_and_index(
    tmp_path_factory: pytest.TempPathFactory, home: str, contents: str, *, staged: bool
) -> None:
    world = World(tmp_path_factory.mktemp("resume") / "project", home)
    baseline = world.tip(home)
    assert baseline is not None
    _run(world, "run-a", 0)
    run_tip = world.tip("vibesys-runs/run-a")
    world.git().switch_branch(home)
    source = world.root / "main.py"
    source.write_text(contents, encoding="utf-8")
    if staged:
        world.git().stage_all(["main.py"])
    loose = world.root / "notes.txt"
    loose.write_text("keep my notes", encoding="utf-8")
    before = (world.git().uncommitted_paths(()), world.git().has_staged_changes())
    resumed = world.tracker("run-a")
    resumed.init(existing=True, trusted_input_baseline=baseline)
    assert world.checked_out() == home
    assert world.tip(home) == baseline
    assert resumed.trusted_input_baseline == baseline
    assert resumed.current_sha() == run_tip
    assert source.read_text(encoding="utf-8") == contents
    assert loose.read_text(encoding="utf-8") == "keep my notes"
    assert (world.git().uncommitted_paths(()), world.git().has_staged_changes()) == before
