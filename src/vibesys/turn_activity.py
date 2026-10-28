"""What one agent turn is doing, derived from its tool calls, tool results and todo list.

One derivation for every reader: the server words it for its live activity event, and the run
record writes it on the turn span (``vibesys.records``). The values are what the agent named
(a tool's raw name, its todo text); wording belongs to whoever shows them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from vs_agent.api import TodoItemData


@dataclass(frozen=True)
class TurnActivity:
    """The tools a turn is running, oldest first, and the todo item it marked in progress."""

    tools: tuple[str, ...] = ()
    todo: str | None = None

    @property
    def tool(self) -> str | None:
        """The newest tool still running, which is what the turn waits on; None between calls."""
        return self.tools[-1] if self.tools else None

    def called(self, tool: str) -> TurnActivity:
        """The turn after it called ``tool``."""
        return TurnActivity((*self.tools, tool), self.todo)

    def returned(self, tool: str) -> TurnActivity:
        """The turn after ``tool`` returned; a result for a tool it is not running changes nothing."""
        if tool not in self.tools:
            return self
        index = self.tools.index(tool)
        return TurnActivity(self.tools[:index] + self.tools[index + 1 :], self.todo)

    def planned(self, todos: Iterable[TodoItemData]) -> TurnActivity:
        """The turn after it reported ``todos``: the first item in progress, else none."""
        current = next((todo.content for todo in todos if todo.status == "in_progress"), None)
        return TurnActivity(self.tools, current)
