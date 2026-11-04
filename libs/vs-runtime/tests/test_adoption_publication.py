"""Generated adoption/resume histories preserve operator edits and publication identity."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from tests.support.workspace_world import open_workspace_env

from vs_core.api import (
    AdoptionObserved,
    AdoptRevision,
    CheckoutStatus,
    DiscardWorkspace,
    ObservationStatus,
    RequestId,
    RetainedCandidate,
    SettlementId,
)
from vs_project.api import GitTracker, NullGitTrackerEvents, adoption_branch
from vs_project.api.testing import FakeGitRepositories
from vs_runtime.api.core import KeyedDocuments
from vs_sim.api.testing import InlineBlockingRunner, SimThreads, examples

from .test_adoption_commit import _SCOPE, _executor
from .test_workspace_requests import _attempt, _common, _ensure, _run, _snapshot_of, _status

if TYPE_CHECKING:
    from pathlib import Path

    from vs_core.api import AdoptionPublication
    from vs_project.api import GitRepository
    from vs_runtime.api.infrastructure import RuntimeWorkspaces


_ACTION = st.sampled_from(("clean", "tracked", "staged", "untracked", "ignored"))
_HISTORY = st.lists(st.tuples(_ACTION, st.booleans()), min_size=1, max_size=8)


def _edit(repository: GitRepository, path: Path, action: str, index: int) -> None:
    if action == "clean":
        repository.reset_index()
        repository.restore_worktree("HEAD", exclude=(".vibesys",))
        (path / "notes.txt").unlink(missing_ok=True)
    elif action in {"tracked", "staged"}:
        (path / "candidate.py").write_text(f"operator edit {index}", encoding="utf-8")
        if action == "staged":
            repository.stage_all(["candidate.py"])
    elif action == "untracked":
        (path / "notes.txt").write_text(f"operator notes {index}", encoding="utf-8")
    else:
        (path / "cache.txt").write_text(f"operator cache {index}", encoding="utf-8")


async def _publish_and_update(
    world: RuntimeWorkspaces,
    repository: GitRepository,
    index: int,
) -> AdoptionPublication:
    path = world.root.path
    candidate = await world.create_candidate(member_id=f"candidate-{index}")
    winner = f"winner {index}"
    (candidate.path / "candidate.py").write_text(winner, encoding="utf-8")
    revision = await candidate.snapshot_and_retain("winner", retention_label="candidate")
    await candidate.discard()
    files = {
        name: (path / name).read_bytes()
        for name in ("candidate.py", "notes.txt", "cache.txt")
        if (path / name).exists()
    }
    head, pending, staged = (
        repository.head(),
        repository.uncommitted_paths([".", ":(exclude).vibesys"]),
        repository.has_staged_changes(),
    )
    published = await world.adopt(revision)
    assert published.branch == adoption_branch("workspace-requests")
    assert await world.adopt(revision) == published
    assert repository.head() == head
    assert repository.uncommitted_paths([".", ":(exclude).vibesys"]) == pending
    assert repository.has_staged_changes() == staged
    assert all((path / name).read_bytes() == content for name, content in files.items())
    assert repository.read_blob(published.commit, "candidate.py") == winner.encode()
    checkout = await world.update_checkout(published)
    if pending:
        assert checkout.status is CheckoutStatus.DIRTY_TREE
        assert repository.head() == head
        assert all((path / name).read_bytes() == content for name, content in files.items())
    else:
        assert checkout.status is CheckoutStatus.FAST_FORWARDED
        assert repository.head() == published.commit
        assert (path / "candidate.py").read_text() == winner
    for name in ("notes.txt", "cache.txt"):
        if name in files:
            assert (path / name).read_bytes() == files[name]
    return published


@pytest.mark.regression
@settings(
    max_examples=examples(pr=5, nightly=40),
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(history=_HISTORY)
@example(history=[("tracked", True), ("staged", True), ("clean", True), ("ignored", False)])
def test_adoption_resume_histories_preserve_every_operator_edit(
    tmp_path: Path,
    history: list[tuple[str, bool]],
) -> None:
    root = tmp_path / str(len(list(tmp_path.iterdir())))
    root.mkdir()

    async def exercise() -> None:
        with open_workspace_env(root) as env:
            disk = env.git_disk
            assert isinstance(disk, FakeGitRepositories)
            world = env.hosts[0]
            repository = disk.repository(world.root.path)
            path = world.root.path
            repository.add_excludes(["cache.txt"])
            try:
                for index, (action, resume) in enumerate(history):
                    _edit(repository, path, action, index)
                    if resume:
                        await world.close()
                        world = env.start_host(InlineBlockingRunner())
                        # Reopening the tracker must preserve staged and unstaged content too.
                        tracker = GitTracker(
                            path,
                            run_id="workspace-requests",
                            events=NullGitTrackerEvents(),
                            repository=disk.repository(path),
                        )
                        tracker.init(existing=True)
                    published = await _publish_and_update(world, repository, index)
                    revision = published.revision
                    await world.close()
                    world = env.start_host(InlineBlockingRunner())
                    assert await world.adopt(revision) == published
            finally:
                for host in reversed(env.hosts):
                    await host.close()

    asyncio.run(exercise())


@pytest.mark.regression
def test_f17_resumed_epoch_adopts_with_dirty_staged_operator_files(tmp_path: Path) -> None:
    async def exercise() -> None:
        with open_workspace_env(tmp_path) as env:
            world = env.hosts[0]
            store = KeyedDocuments(env.receipts_namespace(), threads=SimThreads())
            executor = _executor(world, store)
            attempt = _attempt()
            try:
                assert (
                    _status(await _run(executor, _ensure(world, attempt, "ensure")))
                    is ObservationStatus.SUCCEEDED
                )
                disk = env.git_disk
                assert isinstance(disk, FakeGitRepositories)
                (candidate_path,) = [
                    p for p in disk.worktrees(world.root.path) if p != world.root.path
                ]
                (candidate_path / "candidate.py").write_text("winner", encoding="utf-8")
                revision = await _snapshot_of(executor, attempt, "winner")

                await _run(
                    executor, DiscardWorkspace(**_common(attempt, "discard"), attempt=attempt)
                )
                path = world.root.path
                repository = disk.repository(path)
                (path / "candidate.py").write_text("operator staged", encoding="utf-8")
                repository.stage_all(["candidate.py"])
                (path / "candidate.py").write_text("operator unstaged", encoding="utf-8")
                head = repository.head()
                await world.close()
                world = env.start_host(InlineBlockingRunner())
                executor = _executor(
                    world, KeyedDocuments(env.receipts_namespace(), threads=SimThreads())
                )
                request = AdoptRevision(
                    request_id=RequestId(root="resumed-adoption"),
                    scope=_SCOPE,
                    selection=RetainedCandidate(
                        settlement_id=SettlementId(root="winner"), revision=revision
                    ),
                )
                result = await _run(executor, request, repeat=True)
                assert _status(result) is ObservationStatus.SUCCEEDED
                (event,) = result.owner_events
                assert isinstance(event, AdoptionObserved)
                assert event.publication is not None
                assert event.checkout is not None
                assert event.checkout.status is CheckoutStatus.DIRTY_TREE
                # The report alone says the same, which is all a step's fold keeps of the event.
                assert result.observation.publication == event.publication
                assert result.observation.checkout == event.checkout
                assert repository.read_blob(event.publication.commit, "candidate.py") == b"winner"
                assert repository.head() == head
                assert repository.has_staged_changes()
                assert (path / "candidate.py").read_text() == "operator unstaged"
                assert await _run(executor, request, repeat=True) == result
            finally:
                for host in reversed(env.hosts):
                    await host.close()

    asyncio.run(exercise())
