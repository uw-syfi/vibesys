"""``WorkspaceHandle.transaction()`` commit/restore semantics.

Exercised against a fake ``_Workspaces``-shaped owner (only ``_snapshot`` and
``_restore`` are called by ``WorkspaceHandle``), so these tests do not need a
real Git-backed run. The fake owner models a workspace as a small in-memory
``dict`` tree and mirrors ``GitTracker.checkout_tree``'s ``preserve_paths``
contract: a restore reverts every path to the target snapshot's content
except paths named in ``preserve_paths``, which keep whatever the tree holds
right before the restore.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.orchestration.runtime import (
    WorkspaceHandle,
    WorkspaceRestoreError,
    WorkspaceTransactionKeep,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


class _FakeOwner:
    """Mimics ``_Workspaces``' ``_snapshot``/``_restore`` over an in-memory tree."""

    def __init__(self) -> None:
        self.tree: dict[str, str] = {}
        self._snapshots: dict[str, dict[str, str]] = {}
        self._next_id = 0
        self.restore_calls: list[str] = []
        self.fail_restore_to: str | None = None

    async def _snapshot(self, label: str, scope: Any = None) -> str:  # noqa: ANN401
        del label, scope
        self._next_id += 1
        revision = f"r{self._next_id}"
        self._snapshots[revision] = dict(self.tree)
        return revision

    async def _restore(
        self,
        revision: str,
        *,
        scope: Any = None,  # noqa: ANN401
        clean: bool = True,
        preserve_paths: tuple[str, ...] = (),
    ) -> None:
        del scope, clean
        self.restore_calls.append(revision)
        if revision == self.fail_restore_to:
            raise WorkspaceRestoreError(revision)
        preserved = {path: self.tree[path] for path in preserve_paths if path in self.tree}
        self.tree = dict(self._snapshots[revision])
        self.tree.update(preserved)


def _handle(owner: _FakeOwner) -> WorkspaceHandle:
    return WorkspaceHandle(owner, None)  # ty: ignore[invalid-argument-type]  # test double


class _MarkerError(Exception):
    """A plain exception with no special transaction meaning."""


class _KeepError(WorkspaceTransactionKeep):
    """A body error that must propagate without triggering a restore."""


async def _run(
    owner: _FakeOwner,
    *,
    edits: Mapping[str, str],
    commit: bool,
    raise_mode: str | None,
    preserve: tuple[str, ...] = (),
) -> None:
    """Apply ``edits`` inside one transaction, then optionally commit/raise."""
    handle = _handle(owner)
    async with handle.transaction(preserve=preserve) as tx:
        owner.tree.update(edits)
        if commit:
            tx.commit()
        if raise_mode == "plain":
            raise _MarkerError
        if raise_mode == "keep":
            raise _KeepError


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_keeps_the_body_s_tree() -> None:
    owner = _FakeOwner()
    owner.tree = {"a": "0"}
    await _run(owner, edits={"a": "1", "b": "2"}, commit=True, raise_mode=None)
    assert owner.tree == {"a": "1", "b": "2"}
    assert owner.restore_calls == []


@pytest.mark.asyncio
async def test_exit_without_commit_restores_the_snapshot() -> None:
    owner = _FakeOwner()
    owner.tree = {"a": "0"}
    await _run(owner, edits={"a": "1", "b": "2"}, commit=False, raise_mode=None)
    assert owner.tree == {"a": "0"}
    assert len(owner.restore_calls) == 1


@pytest.mark.asyncio
async def test_exception_restores_the_snapshot_and_propagates() -> None:
    owner = _FakeOwner()
    owner.tree = {"a": "0"}
    with pytest.raises(_MarkerError):
        await _run(owner, edits={"a": "1"}, commit=False, raise_mode="plain")
    assert owner.tree == {"a": "0"}
    assert len(owner.restore_calls) == 1


@pytest.mark.asyncio
async def test_committed_then_raised_still_keeps_the_tree() -> None:
    """A commit locks in the outcome even if the body later raises."""
    owner = _FakeOwner()
    owner.tree = {"a": "0"}
    with pytest.raises(_MarkerError):
        await _run(owner, edits={"a": "1"}, commit=True, raise_mode="plain")
    assert owner.tree == {"a": "1"}
    assert owner.restore_calls == []


@pytest.mark.asyncio
async def test_keep_signal_skips_restore_but_still_propagates() -> None:
    owner = _FakeOwner()
    owner.tree = {"a": "0"}
    with pytest.raises(_KeepError):
        await _run(owner, edits={"a": "1"}, commit=False, raise_mode="keep")
    assert owner.tree == {"a": "1"}
    assert owner.restore_calls == []


async def _mutate_then_fail_restore(owner: _FakeOwner) -> None:
    handle = _handle(owner)
    async with handle.transaction() as tx:
        owner.tree["a"] = "1"
        owner.fail_restore_to = "r1"
        del tx  # not committed: exit must attempt the restore


@pytest.mark.asyncio
async def test_restore_failure_raises_workspace_restore_error() -> None:
    owner = _FakeOwner()
    owner.tree = {"a": "0"}
    with pytest.raises(WorkspaceRestoreError):
        await _mutate_then_fail_restore(owner)


@pytest.mark.asyncio
async def test_preserve_paths_survive_the_exit_restore() -> None:
    owner = _FakeOwner()
    owner.tree = {"memory.md": "old memory", "code.py": "old code"}
    await _run(
        owner,
        edits={"memory.md": "new memory", "code.py": "new code"},
        commit=False,
        raise_mode=None,
        preserve=("memory.md",),
    )
    # code.py reverted to the snapshot; memory.md kept its live edit.
    assert owner.tree == {"memory.md": "new memory", "code.py": "old code"}


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------

_keys = st.sampled_from(["a", "b", "c", "memory.md"])
_edits_strategy = st.dictionaries(_keys, st.text(min_size=1, max_size=4), max_size=4)


@given(
    entry=st.dictionaries(_keys, st.text(min_size=1, max_size=4), max_size=4),
    edits=_edits_strategy,
    commit=st.booleans(),
    raise_mode=st.sampled_from([None, "plain", "keep"]),
    preserve=st.lists(_keys, max_size=2, unique=True),
)
@settings(max_examples=100, deadline=None)
def test_transaction_outcome_matches_commit_or_snapshot(
    entry: dict[str, str],
    edits: dict[str, str],
    commit: bool,  # noqa: FBT001  # a hypothesis-generated scenario parameter
    raise_mode: str | None,
    preserve: list[str],
) -> None:
    """Post-state always equals either the committed tree, or the entry
    snapshot with declared/preserved paths kept live -- regardless of any
    mix of edits, a commit, and a plain or keep-signal exception.
    """
    owner = _FakeOwner()
    owner.tree = dict(entry)
    preserve_tuple = tuple(preserve)

    async def scenario() -> None:
        await _run(
            owner, edits=edits, commit=commit, raise_mode=raise_mode, preserve=preserve_tuple
        )

    if raise_mode == "plain":
        with pytest.raises(_MarkerError):
            asyncio.run(scenario())
    elif raise_mode == "keep":
        with pytest.raises(_KeepError):
            asyncio.run(scenario())
    else:
        asyncio.run(scenario())

    body_tree = dict(entry)
    body_tree.update(edits)

    if commit or raise_mode == "keep":
        # The body's own tree is kept verbatim: a commit locks it in, and a
        # keep signal skips the restore entirely.
        assert owner.tree == body_tree
    else:
        # Restored to entry, except preserved paths keep the body's edit.
        expected = dict(entry)
        for path in preserve_tuple:
            if path in body_tree:
                expected[path] = body_tree[path]
            else:
                expected.pop(path, None)
        assert owner.tree == expected
