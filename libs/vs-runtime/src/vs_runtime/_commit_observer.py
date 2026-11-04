"""The observer of a run's confirmed state changes, for display and projections only."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from vs_runtime._core_record import RuntimeRecord


class CommitObserver(Protocol):
    """Hears each runtime record the run confirmed, for display and projections only.

    It runs after the state change is durable and on the loop's thread, and it must not raise.
    ``previous`` is the record this process held before the change: the last durable one,
    the fresh record of a run that had none, or None when it held nothing.
    """

    def committed(self, previous: RuntimeRecord[Any] | None, current: RuntimeRecord[Any]) -> object:
        """One state change became durable."""
        ...


class IgnoreCommits:
    """The observer of a host that shows nothing about commits."""

    def committed(self, previous: RuntimeRecord[Any] | None, current: RuntimeRecord[Any]) -> None:
        """Drop the report."""
        del previous, current
