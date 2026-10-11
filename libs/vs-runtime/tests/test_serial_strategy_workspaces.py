"""Assumptions A1 and A2 of the strategy port design, on the workspace executor.

A1 (runtime half): a revision retained as a non-adoptable `candidate` is a legal base for a
later attempt's workspace. Core's half is `tests/vibesys/orchestration/test_serial_strategy_assumptions.py`.

A2: a serial run can use the exclusive root.

The `single`, `multi` and `issue_queue` strategies run one attempt at a time. On a host
without isolated candidate sandboxes every attempt would use `WorkspaceMode.EXCLUSIVE_ROOT`:
the run's own root directory. That needs the executor to (1) free the root when an attempt
ends, so the next attempt is granted it, and (2) hand the next attempt a root that holds its
base revision, not the previous attempt's edits.

Both properties hold: discarding a root attempt ends its hold, and granting the root restores
the plan's base. See `docs/design/strategies-on-core.md`, section 11, assumption A2.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from vs_core.api import (
    DiscardWorkspace,
    EnsureWorkspace,
    ObservationStatus,
    RetainRevision,
    RevisionRef,
    SnapshotAndRetain,
    WorkspaceMode,
    WorkspacePlan,
)
from vs_runtime.api.core import revision_ref

from .test_workspace_requests import (
    _attempt,
    _candidate_path,
    _common,
    _ensure,
    _executor,
    _run,
    _status,
    _workspaces,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vs_core.api import AttemptRef
    from vs_runtime.api.infrastructure import RuntimeWorkspaces

_ROOT = WorkspaceMode.EXCLUSIVE_ROOT


def test_a_retained_candidate_is_a_base_for_the_next_attempt(tmp_path: Path) -> None:
    """The next round of a serial search builds on a round that was kept but never audited."""

    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces)
        first = _attempt("a1")
        await _run(executor, _ensure(workspaces, first, "ensure-1"))
        candidate_path = _candidate_path(workspaces)
        (candidate_path / "candidate.py").write_text("VALUE = 7\n", encoding="utf-8")
        snapshot = await _run(
            executor, SnapshotAndRetain(**_common(first, "snap-1"), attempt=first, retention="wip")
        )
        revision = snapshot.observation.revision
        assert revision is not None
        kept = await _run(
            executor,
            RetainRevision(
                **_common(first, "retain-1"),
                attempt=first,
                revision=revision,
                retention="candidate",
            ),
        )
        assert _status(kept) is ObservationStatus.SUCCEEDED
        await _run(executor, DiscardWorkspace(**_common(first, "discard-1"), attempt=first))
        second = _attempt("a2")
        plan = WorkspacePlan(mode=WorkspaceMode.ISOLATED_CHILD, base=revision)
        child = await _run(
            executor, EnsureWorkspace(**_common(second, "ensure-2"), attempt=second, plan=plan)
        )
        assert _status(child) is ObservationStatus.SUCCEEDED
        assert child.observation.revision == revision
        content = (_candidate_path(workspaces) / "candidate.py").read_text(encoding="utf-8")
        assert content == "VALUE = 7\n"

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


def _root_ensure(attempt: AttemptRef, name: str, base: RevisionRef) -> EnsureWorkspace:
    plan = WorkspacePlan(mode=_ROOT, base=base)
    return EnsureWorkspace(**_common(attempt, name), attempt=attempt, plan=plan)


async def _edited_and_snapshotted(workspaces: RuntimeWorkspaces) -> RevisionRef:
    """Attempt a1 holds the root, edits a tracked file and snapshots it as `wip`.

    Returns the base a1 started from, kept as a candidate so a later attempt may name it
    after the snapshot moved the root's head.
    """
    executor, _ = _executor(workspaces)
    first = _attempt("a1")
    assert workspaces.root.revision is not None
    base = revision_ref(workspaces.root.revision)
    granted = await _run(executor, _root_ensure(first, "ensure-1", base))
    assert _status(granted) is ObservationStatus.SUCCEEDED
    kept = await _run(
        executor,
        RetainRevision(
            **_common(first, "retain-base"), attempt=first, revision=base, retention="candidate"
        ),
    )
    assert _status(kept) is ObservationStatus.SUCCEEDED
    (workspaces.root.path / "candidate.py").write_text("VALUE = 2\n", encoding="utf-8")
    snapshot = await _run(
        executor, SnapshotAndRetain(**_common(first, "snap-1"), attempt=first, retention="wip")
    )
    assert _status(snapshot) is ObservationStatus.SUCCEEDED
    return base


def test_an_ended_attempt_releases_the_exclusive_root(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        base = await _edited_and_snapshotted(workspaces)
        executor, _ = _executor(workspaces)
        first = _attempt("a1")
        ended = await _run(executor, DiscardWorkspace(**_common(first, "discard-1"), attempt=first))
        assert _status(ended) is ObservationStatus.SUCCEEDED
        second = _attempt("a2")
        again = await _run(executor, _root_ensure(second, "ensure-2", base))
        assert _status(again) is ObservationStatus.SUCCEEDED
        content = (workspaces.root.path / "candidate.py").read_text(encoding="utf-8")
        assert content == "VALUE = 1\n"

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


def test_a_new_generation_of_the_attempt_starts_from_its_base(tmp_path: Path) -> None:
    """The retry path needs no release: the same attempt, next generation, is granted the root."""

    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        base = await _edited_and_snapshotted(workspaces)
        executor, _ = _executor(workspaces)
        retry = _attempt("a1", 1)
        granted = await _run(executor, _root_ensure(retry, "ensure-1b", base))
        assert _status(granted) is ObservationStatus.SUCCEEDED
        content = (workspaces.root.path / "candidate.py").read_text(encoding="utf-8")
        assert content == "VALUE = 1\n"

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))
