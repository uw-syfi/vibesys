"""Fakes for public run handles, launch collections and persisted records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vibesys.api.store import (
    RunDocument,
    RunRecordFacts,
    RunRecordReadError,
    WorkspaceChange,
)
from vs_runtime.api.testing import FakeRunHandle, FakeRuns, FakeStopTimer

if TYPE_CHECKING:
    from vibesys.api.contracts import RunView


@dataclass
class FakeRunRecord:
    """In-memory semantic run record with deterministic history failures."""

    run_id: str
    run_view: RunView
    record_facts: RunRecordFacts
    identity: str = ""
    history: tuple[RunDocument, ...] = ()
    portable: tuple[RunDocument, ...] = ()
    workspace_change_requests: list[tuple[str, str]] = field(default_factory=list, init=False)
    workspace_patch_requests: list[tuple[str, str, tuple[str, ...]]] = field(
        default_factory=list,
        init=False,
    )
    _changes: dict[tuple[str, str], tuple[WorkspaceChange, ...] | RunRecordReadError] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _patches: dict[tuple[str, str, tuple[str, ...]], str | RunRecordReadError] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        """Fill a deterministic cache identity when none was supplied."""
        if not self.identity:
            self.identity = f"fake:{self.run_id}"

    def view(self) -> RunView:
        """Return the configured run projection."""
        return self.run_view

    def facts(self) -> RunRecordFacts:
        """Return the configured manifest facts."""
        return self.record_facts

    def history_documents(self) -> tuple[RunDocument, ...]:
        """Return configured policy-selected history documents."""
        return self.history

    def portable_documents(self) -> tuple[RunDocument, ...]:
        """Return configured portable documents."""
        return self.portable

    def workspace_changes(self, base: str, head: str) -> tuple[WorkspaceChange, ...]:
        """Record and answer one semantic workspace-change request."""
        self.workspace_change_requests.append((base, head))
        result = self._changes.get((base, head), ())
        if isinstance(result, RunRecordReadError):
            raise result
        return result

    def workspace_patch(self, base: str, head: str, paths: tuple[str, ...]) -> str:
        """Record and answer one semantic workspace-patch request."""
        self.workspace_patch_requests.append((base, head, paths))
        result = self._patches.get((base, head, paths), "")
        if isinstance(result, RunRecordReadError):
            raise result
        return result

    def set_workspace_changes(
        self,
        base: str,
        head: str,
        *changes: WorkspaceChange,
    ) -> None:
        """Configure semantic changes for one immutable range."""
        self._changes[(base, head)] = changes

    def set_workspace_patch(
        self,
        base: str,
        head: str,
        paths: tuple[str, ...],
        patch: str,
    ) -> None:
        """Configure patch text for one immutable range and path set."""
        self._patches[(base, head, paths)] = patch

    def fail_workspace_changes(
        self,
        base: str,
        head: str,
        error: RunRecordReadError,
    ) -> None:
        """Configure a typed change-read failure for one range."""
        self._changes[(base, head)] = error

    def fail_workspace_patch(
        self,
        base: str,
        head: str,
        paths: tuple[str, ...],
        error: RunRecordReadError,
    ) -> None:
        """Configure a typed patch-read failure for one range and path set."""
        self._patches[(base, head, paths)] = error


__all__ = ["FakeRunHandle", "FakeRunRecord", "FakeRuns", "FakeStopTimer"]
