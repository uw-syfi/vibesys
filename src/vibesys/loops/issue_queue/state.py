"""Typed persistence adapter for plain-loop state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_loop_state.api import (
    PlainLoopCursor,
    PlainPerformanceRecord,
    PlainPerformanceSnapshot,
)

if TYPE_CHECKING:
    from vs_project.api import StateNamespace, StateSlot

_CURSOR_FILE = "state.json"
_PERFORMANCE_FILE = "perf/metrics.json"


class IssueQueueStateStore:
    """Persist strict plain-loop models inside one portable namespace."""

    def __init__(self, namespace: StateNamespace) -> None:
        """Bind the adapter to one portable plain-loop namespace."""
        self._namespace = namespace
        self._cursor: StateSlot[PlainLoopCursor] = namespace.slot(
            _CURSOR_FILE,
            PlainLoopCursor,
        )
        self._performance: StateSlot[PlainPerformanceSnapshot] = namespace.slot(
            _PERFORMANCE_FILE,
            PlainPerformanceSnapshot,
        )

    def load_cursor(self) -> PlainLoopCursor | None:
        """Load the cursor, returning ``None`` only when it is absent."""
        return self._cursor.load_optional()

    def save_cursor(self, cursor: PlainLoopCursor) -> None:
        """Atomically save the current cursor."""
        self._cursor.save(cursor)

    def load_performance(self) -> PlainPerformanceSnapshot:
        """Load performance history, starting empty when no history exists."""
        return self._performance.load_optional() or PlainPerformanceSnapshot()

    def append_performance(
        self,
        record: PlainPerformanceRecord,
    ) -> PlainPerformanceRecord:
        """Validate and append one performance evaluation."""
        current = self.load_performance()
        updated = PlainPerformanceSnapshot(records=(*current.records, record))
        self._performance.save(updated)
        return record

    @property
    def namespace(self) -> StateNamespace:
        """Return the namespace for committing its validated snapshot."""
        return self._namespace
