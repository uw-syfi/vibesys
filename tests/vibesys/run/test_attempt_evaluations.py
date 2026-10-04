"""Host attempt cursors retain immutable submission ownership across restart."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

from vibesys.run.attempt_evaluations import (
    AttemptEvaluationCursor,
    AttemptEvaluationCursorError,
    AttemptEvaluationCursors,
)
from vs_project.api import FakeStateModels, ProjectStateError

if TYPE_CHECKING:
    from pathlib import PurePosixPath

_IDENTITIES = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789/-_", min_size=1, max_size=80)


@given(
    invocation=_IDENTITIES,
    workspace=_IDENTITIES,
    preceding_handles=st.lists(_IDENTITIES, unique=True, max_size=30).map(tuple),
)
def test_cursor_is_immutable_idempotent_and_survives_reconstruction(
    invocation: str, workspace: str, preceding_handles: tuple[str, ...]
) -> None:
    namespace = FakeStateModels()
    cursors = AttemptEvaluationCursors(namespace)
    assert cursors.read(invocation) is None
    recorded = cursors.record(
        invocation_id=invocation, workspace_id=workspace, preceding_handles=preceding_handles
    )
    restarted = AttemptEvaluationCursors(namespace)
    assert recorded.submitted_before == len(preceding_handles)
    assert restarted.read(invocation) == recorded
    assert (
        restarted.record(
            invocation_id=invocation, workspace_id=workspace, preceding_handles=preceding_handles
        )
        == recorded
    )
    with pytest.raises(AttemptEvaluationCursorError, match="conflicts with invocation"):
        restarted.record(
            invocation_id=invocation,
            workspace_id=workspace,
            preceding_handles=(*preceding_handles, "x" * 81),
        )
    with pytest.raises(AttemptEvaluationCursorError, match="conflicts with invocation"):
        restarted.record(
            invocation_id=invocation,
            workspace_id=workspace + "-other",
            preceding_handles=preceding_handles,
        )
    assert restarted.read(invocation) == recorded


class _PersistedCursorRead:
    """A storage boundary supplying a persisted record or a typed read failure."""

    def __init__(self, cursor: AttemptEvaluationCursor | None = None) -> None:
        self.cursor = cursor

    def load_optional[ModelT: BaseModel](
        self, relative_path: str | PurePosixPath, model_type: type[ModelT]
    ) -> ModelT | None:
        """Validate a returned record through the requested model's public API."""
        assert relative_path
        if self.cursor is None:
            message = "invalid persisted cursor"
            raise ProjectStateError(message)
        return model_type.model_validate(self.cursor.model_dump(), strict=True)

    def save(self, relative_path: str | PurePosixPath, model: BaseModel) -> None:
        """Reject writes in a read-only persistence failure scenario."""
        message = f"unexpected cursor write to {relative_path!s}: {type(model).__name__}"
        raise AssertionError(message)


def test_cursor_propagates_typed_persistence_failure() -> None:
    cursors = AttemptEvaluationCursors(_PersistedCursorRead())
    with pytest.raises(ProjectStateError, match="invalid persisted cursor"):
        cursors.read("turn/1")


def test_persisted_cursor_rejects_conflicting_invocation_identity() -> None:
    namespace = _PersistedCursorRead(
        AttemptEvaluationCursor(
            invocation_id="turn/2", workspace_id="member", preceding_handles=("h0", "h1")
        )
    )
    cursors = AttemptEvaluationCursors(namespace)
    with pytest.raises(AttemptEvaluationCursorError, match="differs from invocation"):
        cursors.read("turn/1")
