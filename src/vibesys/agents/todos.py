"""Translate provider-specific plan/todo tool calls into todo snapshots.

Each CLI provider surfaces the agent's working plan through its own tool
convention: Claude Code calls ``TodoWrite``, opencode calls ``todowrite``,
Gemini calls ``write_todos``, and Codex reports either an ``update_plan``
tool call or a ``todo_list`` stream item. This module owns the mapping from
those provider vocabularies to the neutral :class:`TodoItemData` contract so
everything downstream of the output sink (supervisor, renderers, TUI) stays
agent-agnostic.

The deepagents backend additionally publishes todos from its graph state
channel in :mod:`vibesys.agent_runner`; its ``write_todos`` tool call also
matches here, which is harmless because todo updates are full-list snapshots
and re-publishing the same snapshot is idempotent for every consumer.

Extraction is best-effort by design: payloads originate from agent tool
calls, so a malformed entry is skipped and an unrecognized payload yields
"no update" — never an exception into the agent run. Statuses pass through
as open strings; renderers own the degradation of unknown values (see the
``TodoItemData.status`` contract in :mod:`vibesys.server.events`).
"""

from collections.abc import Callable, Mapping
from typing import Any

from vibesys.server.events import TodoItemData

_Extractor = Callable[[Mapping[str, Any]], list[TodoItemData] | None]

_COMPLETED = "completed"
_PENDING = "pending"


def todos_from_tool_call(tool: str, args: Mapping[str, Any]) -> list[TodoItemData] | None:
    """Return the plan snapshot carried by a *tool* invocation.

    Returns ``None`` when *tool* is not a recognized todo/plan tool or the
    payload does not carry a todo list at all; returns a (possibly empty)
    snapshot otherwise. Callers publish snapshots and ignore ``None``.
    """
    extractor = _EXTRACTORS.get(tool)
    if extractor is None:
        return None
    return extractor(args)


def _coerce_item(entry: object, content_keys: tuple[str, ...]) -> TodoItemData | None:
    """Build one todo item from an untrusted payload entry.

    ``content_keys`` are tried in order because providers name the item text
    differently (``content``, ``step``, ``text``, …). Entries without usable
    text are dropped: an unlabeled todo renders as an empty row and carries
    no information.
    """
    if not isinstance(entry, Mapping):
        return None
    content = next(
        (
            value
            for key in content_keys
            if isinstance(value := entry.get(key), str) and value.strip()
        ),
        None,
    )
    if content is None:
        return None
    status = entry.get("status")
    if isinstance(status, str) and status:
        return TodoItemData(content=content, status=status)
    completed = entry.get("completed")
    if isinstance(completed, bool):
        return TodoItemData(content=content, status=_COMPLETED if completed else _PENDING)
    return TodoItemData(content=content, status=_PENDING)


def _extract(
    args: Mapping[str, Any], list_key: str, content_keys: tuple[str, ...]
) -> list[TodoItemData] | None:
    raw = args.get(list_key)
    if not isinstance(raw, list):
        return None
    entries: list[Any] = raw
    items = (_coerce_item(entry, content_keys) for entry in entries)
    return [item for item in items if item is not None]


def _from_todos_arg(args: Mapping[str, Any]) -> list[TodoItemData] | None:
    """Claude Code ``TodoWrite`` / opencode ``todowrite`` / Gemini and
    deepagents ``write_todos``: ``{"todos": [{"content", "status"}, …]}``."""
    return _extract(args, "todos", ("content", "description"))


def _from_plan_arg(args: Mapping[str, Any]) -> list[TodoItemData] | None:
    """Codex ``update_plan`` tool: ``{"plan": [{"step", "status"}, …]}``."""
    return _extract(args, "plan", ("step",))


def _from_items_arg(args: Mapping[str, Any]) -> list[TodoItemData] | None:
    """Codex ``exec --json`` ``todo_list`` stream item:
    ``{"items": [{"text", "completed"|"status"}, …]}``."""
    return _extract(args, "items", ("text",))


_EXTRACTORS: dict[str, _Extractor] = {
    "TodoWrite": _from_todos_arg,
    "todowrite": _from_todos_arg,
    "write_todos": _from_todos_arg,
    "update_plan": _from_plan_arg,
    "todo_list": _from_items_arg,
}
