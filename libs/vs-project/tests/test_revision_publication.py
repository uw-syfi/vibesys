"""Run-owned publications outlive checkouts, restarts and optional checkout refusals."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import pytest

from vs_project.api import (
    CheckoutOutcome,
    GitCommandError,
    GitTracker,
    NullGitTrackerEvents,
    PublicationError,
)
from vs_project.api.testing import ContractProject, FakeGitRepositories

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _World:
    project: ContractProject
    candidate: ContractProject
    tracker: GitTracker

    def winner(self, content: str) -> str:
        self.candidate.write("code.py", content)
        revision = self.candidate.commit_all(content)
        self.tracker.retain_candidate(content, revision)
        return revision


@pytest.fixture
def world(tmp_path: Path) -> _World:
    disk = FakeGitRepositories()
    root = tmp_path / "root"
    root.mkdir()
    project = ContractProject(root, disk)
    project.start({"code.py": "input"})
    tracker = GitTracker(root, run_id="run", events=NullGitTrackerEvents(), repository=project.repo)
    tracker.init(existing=False)
    baseline = project.repo.head()
    assert baseline is not None
    candidate_root = tmp_path / "candidate"
    project.repo.add_worktree(candidate_root, baseline)
    candidate = ContractProject(candidate_root, disk)
    return _World(project, candidate, tracker)


def test_resumed_publication_is_idempotent_and_later_winners_only_advance(world: _World) -> None:
    first_revision = world.winner("first")
    first = world.tracker.publish_revision(first_revision, "first")
    second_revision = world.winner("second")
    second = world.tracker.publish_revision(second_revision, "second")
    assert world.project.repo.is_ancestor(first.commit, second.commit)
    assert world.project.repo.resolve_commit(first.branch) == second.commit
    world.tracker.init(existing=True)
    assert world.tracker.publish_revision(first_revision, "different label") == first
    assert world.tracker.publish_revision(second_revision, "another label") == second
    assert world.project.repo.resolve_commit(first.branch) == second.commit
    assert (world.project.root / "code.py").read_text() == "input"


def test_publication_checked_out_by_operator_is_never_moved_and_retry_reuses_commit(
    world: _World,
) -> None:
    first = world.tracker.publish_revision(world.winner("first"), "first")
    world.project.repo.switch_branch(first.branch)
    second_revision = world.winner("second")
    with pytest.raises(PublicationError, match="candidate retained"):
        world.tracker.publish_revision(second_revision, "second")
    assert world.project.repo.head() == first.commit
    assert (world.project.root / "code.py").read_text() == "first"
    world.project.repo.switch_branch(world.tracker.project_branch)
    second = world.tracker.publish_revision(second_revision, "second")
    assert world.project.repo.is_ancestor(first.commit, second.commit)
    assert world.tracker.publish_revision(second_revision, "retry") == second


def test_checkout_refusals_do_not_change_the_published_result(world: _World) -> None:
    publication = world.tracker.publish_revision(world.winner("winner"), "winner")
    world.project.repo.switch_branch("main")
    assert world.tracker.update_checkout(publication) is CheckoutOutcome.OTHER_BRANCH
    world.project.repo.switch_branch(world.tracker.project_branch)
    world.project.write("notes", "operator notes")
    assert world.tracker.update_checkout(publication) is CheckoutOutcome.DIRTY_TREE
    world.project.delete("notes")
    assert world.tracker.update_checkout(publication) is CheckoutOutcome.FAST_FORWARDED
    world.project.write("code.py", "operator commit")
    operator = world.project.commit_all("operator")
    assert world.tracker.update_checkout(publication) is CheckoutOutcome.OPERATOR_CHANGED_CHECKOUT
    assert world.project.repo.head() == operator
    assert world.project.repo.resolve_commit(publication.branch) == publication.commit
    assert world.tracker.publish_revision(publication.revision, "retry") == publication


def test_checkout_rejects_a_forged_publication(world: _World) -> None:
    publication = world.tracker.publish_revision(world.winner("winner"), "winner")
    forged = replace(publication, commit="0" * 40)
    with pytest.raises(PublicationError):
        world.tracker.update_checkout(forged)


@pytest.mark.regression
def test_resume_on_an_operator_branch_publishes_without_moving_its_files_or_index(
    world: _World,
) -> None:
    revision = world.winner("winner")
    baseline = world.tracker.trusted_input_baseline
    world.project.repo.switch_branch("main")
    world.project.write("code.py", "operator staged")
    world.project.repo.stage_all(["code.py"])
    world.project.write("code.py", "operator unstaged")
    operator_head = world.project.repo.head()
    resumed = GitTracker(
        world.project.root,
        run_id="run",
        events=NullGitTrackerEvents(),
        repository=world.project.repo,
    )
    resumed.init(existing=True, trusted_input_baseline=baseline)
    publication = resumed.publish_revision(revision, "winner")
    assert resumed.update_checkout(publication) is CheckoutOutcome.OTHER_BRANCH
    assert world.project.repo.current_branch() == "main"
    assert world.project.repo.head() == operator_head
    assert world.project.repo.has_staged_changes()
    assert (world.project.root / "code.py").read_text() == "operator unstaged"
    assert world.project.repo.read_blob(publication.commit, "code.py") == b"winner"


def test_git_checkout_refusal_keeps_publication_and_can_be_retried(world: _World) -> None:
    publication = world.tracker.publish_revision(world.winner("winner"), "winner")
    disk = world.project.factory
    assert isinstance(disk, FakeGitRepositories)
    before = world.project.repo.head()
    disk.fail_next("fast_forward_checkout", GitCommandError(["merge"], 1, "checkout refused"))
    assert world.tracker.update_checkout(publication) is CheckoutOutcome.GIT_REFUSED
    assert world.project.repo.head() == before
    assert world.project.repo.resolve_commit(publication.branch) == publication.commit
    assert world.tracker.update_checkout(publication) is CheckoutOutcome.FAST_FORWARDED


def test_operator_commits_on_publication_branch_are_not_used_as_parents(world: _World) -> None:
    first = world.tracker.publish_revision(world.winner("first"), "first")
    world.project.repo.switch_branch(first.branch)
    world.project.write("code.py", "operator publication edit")
    operator = world.project.commit_all("operator publication edit")
    world.project.repo.switch_branch(world.tracker.project_branch)
    revision = world.winner("second")
    with pytest.raises(PublicationError):
        world.tracker.publish_revision(revision, "second")
    assert world.project.repo.resolve_commit(first.branch) == operator
    assert world.project.repo.read_blob(operator, "code.py") == b"operator publication edit"
