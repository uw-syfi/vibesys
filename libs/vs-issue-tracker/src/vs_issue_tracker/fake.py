"""In-memory issue tracker with scripted faults, for tests of code that uses a tracker.

It is the real board logic held in memory, so it is exactly as strict as the file-backed
board. A fault hook is called around every operation and may raise to simulate a failure
on either side of the effect: before it (the write did not happen) or after it (the write
happened and its acknowledgement was lost).
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, Literal

from vs_issue_tracker.core import Issue, IssueStatus, IssueType, _MemoryBoard

if TYPE_CHECKING:
    import builtins
    from collections.abc import Callable

type FaultPhase = Literal["before", "after"]
type FaultHook = Callable[[str, FaultPhase], None]
"""Called as ``hook(operation, phase)``; raising from it fails the operation at that point."""


class FakeIssueTracker(_MemoryBoard):
    """An in-memory `IssueTracker` whose operations can fail on either side of their effect."""

    def __init__(self, *, faults: FaultHook | None = None) -> None:
        """Open an empty board; ``faults`` is consulted around every operation."""
        super().__init__()
        self.faults = faults

    def __deepcopy__(self, memo: dict[int, object]) -> FakeIssueTracker:
        """An independent board with the same issues; the lock is the one thing not copied."""
        clone = type(self).__new__(type(self))
        memo[id(self)] = clone
        for name, value in vars(self).items():
            setattr(clone, name, type(value)() if name == "_lock" else copy.deepcopy(value, memo))
        return clone

    def _around[T](self, operation: str, perform: Callable[[], T]) -> T:
        if self.faults is not None:
            self.faults(operation, "before")
        result = perform()
        if self.faults is not None:
            self.faults(operation, "after")
        return result

    def create(  # noqa: PLR0913  # lint-waiver: LW-771018 [PLR0913]; the storage-neutral create contract names these independent fields.
        self,
        *,
        type: IssueType | str,  # noqa: A002  # lint-waiver: LW-771019 [A002]; preserve the storage-neutral tracker create keyword.
        title: str,
        description: str,
        created_by: str,
        iteration: int,
        key: str | None = None,
    ) -> Issue:
        """Create an issue, failing as scripted."""
        return self._around(
            "create",
            lambda: super(FakeIssueTracker, self).create(
                type=type,
                title=title,
                description=description,
                created_by=created_by,
                iteration=iteration,
                key=key,
            ),
        )

    def get(self, issue_id: int) -> Issue | None:
        """Read one issue, failing as scripted."""
        return self._around("get", lambda: super(FakeIssueTracker, self).get(issue_id))

    def update_status(  # noqa: PLR0913  # lint-waiver: LW-771020 [PLR0913]; the storage-neutral transition contract names these independent fields.
        self,
        issue_id: int,
        status: IssueStatus | str,
        *,
        actor: str,
        iteration: int,
        note: str = "",
        payload: dict[str, Any] | None = None,
        key: str | None = None,
    ) -> Issue:
        """Change a status, failing as scripted."""
        return self._around(
            "update_status",
            lambda: super(FakeIssueTracker, self).update_status(
                issue_id,
                status,
                actor=actor,
                iteration=iteration,
                note=note,
                payload=payload,
                key=key,
            ),
        )

    def increment_attempts(  # noqa: PLR0913  # lint-waiver: LW-771021 [PLR0913]; the storage-neutral attempt contract names these independent fields.
        self,
        issue_id: int,
        *,
        actor: str,
        iteration: int,
        note: str = "",
        payload: dict[str, Any] | None = None,
        key: str | None = None,
    ) -> Issue:
        """Record an attempt, failing as scripted."""
        return self._around(
            "increment_attempts",
            lambda: super(FakeIssueTracker, self).increment_attempts(
                issue_id, actor=actor, iteration=iteration, note=note, payload=payload, key=key
            ),
        )

    def list(
        self,
        *,
        status: IssueStatus | str | None = None,
        type: IssueType | str | None = None,  # noqa: A002  # lint-waiver: LW-771022 [A002]; preserve the storage-neutral filter keyword.
    ) -> builtins.list[Issue]:
        """List issues, failing as scripted."""
        return self._around(
            "list", lambda: super(FakeIssueTracker, self).list(status=status, type=type)
        )


__all__ = ["FakeIssueTracker", "FaultHook", "FaultPhase"]
