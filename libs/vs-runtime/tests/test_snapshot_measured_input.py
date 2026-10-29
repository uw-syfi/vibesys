"""A snapshot's observation names the measured input of its revision (#2949).

Core keys an official measurement's reuse on it: a commit that changes only paths the task
declares unread (here ``NOTES.md``, as the test environment's evaluation plan declares) is
the input an earlier run already measured.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from vs_core.api import (
    InvocationId,
    InvocationRef,
    SessionId,
    SnapshotAndRetain,
    WorkspaceMode,
)

from .test_workspace_requests import (
    _attempt,
    _candidate_path,
    _common,
    _ensure,
    _executor,
    _run,
    _workspaces,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.api.infrastructure import RuntimeWorkspaces


def test_a_notes_only_commit_keeps_the_measured_input_and_a_code_change_moves_it(
    tmp_path: Path,
) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces)
        attempt = _attempt()
        await _run(executor, _ensure(workspaces, attempt, "ensure-1"))
        path = _candidate_path(workspaces)
        ref = InvocationRef(
            session_id=SessionId(root="implementer"),
            invocation_id=InvocationId(root="turn-1"),
            generation=0,
        )
        seen = []
        for number, (name, text) in enumerate(
            (
                ("candidate.py", "VALUE = 7\n"),
                ("NOTES.md", "tried 7\n"),
                ("candidate.py", "VALUE = 8\n"),
            )
        ):
            (path / name).write_text(text, encoding="utf-8")
            snapshot = SnapshotAndRetain(
                **_common(attempt, f"snap-{number}"),
                attempt=attempt,
                retention="wip",
                invocation=ref,
            )
            observed = (await _run(executor, snapshot)).observation
            assert observed.revision is not None
            assert observed.observation.measured_input is not None
            seen.append((observed.revision, observed.observation.measured_input))
        (code, first), (notes, second), (changed, third) = seen
        assert code != notes, "the notes are committed: a new revision"
        assert first == second, "and the same measured input"
        assert changed != notes
        assert third != second

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


def test_a_measured_candidate_holds_the_revision_less_its_unread_paths(tmp_path: Path) -> None:
    """What an evaluation reads is what the measured input digests: no ``NOTES.md``."""

    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces)
        attempt = _attempt()
        await _run(executor, _ensure(workspaces, attempt, "ensure-1"))
        path = _candidate_path(workspaces)
        (path / "candidate.py").write_text("VALUE = 7\n", encoding="utf-8")
        (path / "NOTES.md").write_text("tried 7\n", encoding="utf-8")
        snapshot = SnapshotAndRetain(
            **_common(attempt, "snap-notes"),
            attempt=attempt,
            retention="wip",
            invocation=InvocationRef(
                session_id=SessionId(root="implementer"),
                invocation_id=InvocationId(root="turn-1"),
                generation=0,
            ),
        )
        revision = (await _run(executor, snapshot)).observation.revision
        assert revision is not None
        commit = revision.git_commit
        assert commit is not None
        agents = await workspaces.create_candidate(commit)
        measured = await workspaces.create_measured_candidate(commit)
        try:
            assert (agents.path / "NOTES.md").read_text(encoding="utf-8") == "tried 7\n"
            assert not (measured.path / "NOTES.md").exists()
            assert (measured.path / "candidate.py").read_text(encoding="utf-8") == "VALUE = 7\n"
        finally:
            await agents.discard()
            await measured.discard()

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


@pytest.mark.parametrize("mode", [WorkspaceMode.ISOLATED_CHILD, WorkspaceMode.EXCLUSIVE_ROOT])
def test_an_ensured_workspace_names_the_measured_input_of_its_base(
    tmp_path: Path, mode: WorkspaceMode
) -> None:
    """Core compares a candidate with the attempt's base, whose input only the runtime digests.

    The ensure's observation carries what an evaluation reads at the base, the digest a
    snapshot of that revision would carry, in either workspace mode.
    """

    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces)
        attempt = _attempt()
        ensure = _ensure(workspaces, attempt, "ensure-1", mode)
        observed = (await _run(executor, ensure)).observation
        base = ensure.plan.base.git_commit
        assert base is not None
        assert observed.observation.measured_input == await workspaces.root.measured_input(base)
        assert observed.observation.measured_input is not None

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))
