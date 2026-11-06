"""A provider's tool call as the call a client shows.

agentshim hands over a tool's name and arguments as the provider spelled them: Claude's ``Bash``
takes ``command``, Codex's ``execute`` takes ``command`` and its ``exec_command`` takes ``cmd``,
and a file edit is ``Edit`` for one and ``file_change`` for the other. This is the one place that
knows those spellings. It gives each call exactly one body of ``ToolCall``: a command, a file read,
write or edit, a search, or, for any tool it does not name, ``OtherCall`` with the tool's own name
and its arguments as text. A tool of our own MCP server is named plainly (``spawn_agent``), not as
the harness prefixes it, so no client learns a provider's spelling. A call is never dropped, and no argument is passed on as raw JSON.

The mapping is pure: the same name and arguments always give the same call.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Final

from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch

#: Tools that run a command, by lower-cased name.
_SHELL: Final = frozenset(
    {"bash", "shell", "execute", "exec", "exec_command", "local_shell", "run_shell_command"}
)
_READ: Final = frozenset({"read", "read_file", "view"})
_WRITE: Final = frozenset({"write", "write_file"})
_EDIT: Final = frozenset(
    {"edit", "multiedit", "notebookedit", "file_change", "apply_patch", "replace", "str_replace"}
)
_SEARCH: Final = frozenset({"grep", "glob", "grep_search", "search_file_content"})
#: Tools whose arguments name another tool, which is what a person wants to read.
_WRAPPED: Final = frozenset({"mcp_tool_call", "dynamic_tool_call"})

_PATH_KEYS: Final = ("file_path", "path", "absolute_path", "notebook_path")
_COMMAND_KEYS: Final = ("command", "cmd")
_PATTERN_KEYS: Final = ("pattern", "query")
_UNNAMED: Final = "unknown"
#: The name our tool server goes by (``internal/mcp.ServerName``). Claude shows its tools as
#: ``mcp__<server>__<tool>``; Codex hands the server and the tool apart, so it needs no stripping.
_OWN_PREFIX: Final = "mcp__vibesys__"


def describe_call(tool: str, args: object) -> orch.ToolCall:
    """The call a provider's *tool* with *args* is, with no output yet."""
    name = tool.strip() or _UNNAMED
    arguments = args if isinstance(args, Mapping) else None
    key = name.lower()
    call = orch.ToolCall()
    if key in _SHELL and (command := _shell_command(args, arguments)) is not None:
        call.shell.command = command
    elif key in _READ and (path := _path(args, arguments)) is not None:
        call.read.target = path
    elif key in _WRITE and (path := _path(args, arguments)) is not None:
        call.write.target = path
    elif key in _EDIT and (paths := _edited_paths(args, arguments)) is not None:
        call.edit.targets.extend(paths)
    elif key in _SEARCH and (pattern := _pattern(args, arguments)) is not None:
        call.search.pattern = pattern
    else:
        shown_name, shown_args = _other(name, key, args, arguments)
        call.other.name = shown_name
        call.other.text = _text(shown_args)
    return call


def _unsaid(args: object) -> bool:
    """Whether the provider has not yet said what the tool is asked: a call starts with no arguments."""
    return args is None or args == "" or (isinstance(args, Mapping) and not args)


def _shell_command(args: object, arguments: Mapping[str, object] | None) -> str | None:
    """The command, ``""`` while it is unsaid, or None when the arguments hold none."""
    if _unsaid(args):
        return ""
    for key in _COMMAND_KEYS:
        value = arguments.get(key) if arguments is not None else None
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return " ".join(str(part) for part in value)
    return None


def _path(args: object, arguments: Mapping[str, object] | None) -> str | None:
    if _unsaid(args):
        return ""
    return _first_text(arguments, _PATH_KEYS) or None


def _pattern(args: object, arguments: Mapping[str, object] | None) -> str | None:
    if _unsaid(args):
        return ""
    return _first_text(arguments, _PATTERN_KEYS) or None


def _first_text(arguments: Mapping[str, object] | None, keys: tuple[str, ...]) -> str:
    if arguments is None:
        return ""
    for key in keys:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _edited_paths(args: object, arguments: Mapping[str, object] | None) -> list[str] | None:
    if _unsaid(args):
        return []
    path = _first_text(arguments, _PATH_KEYS)
    if path:
        return [path]
    changes = arguments.get("changes") if arguments is not None else None
    if not isinstance(changes, list):
        return None
    paths = [
        change["path"]
        for change in changes
        if isinstance(change, Mapping) and isinstance(change.get("path"), str) and change["path"]
    ]
    return paths or None


def _other(
    name: str, key: str, args: object, arguments: Mapping[str, object] | None
) -> tuple[str, object]:
    if key in _WRAPPED and arguments is not None:
        inner = arguments.get("tool")
        if isinstance(inner, str) and inner:
            return _own_name(inner), arguments.get("arguments")
    return _own_name(name), arguments if arguments is not None else args


def _own_name(name: str) -> str:
    """A tool of our server by its plain name (``spawn_agent``), however the harness prefixes it."""
    return name.removeprefix(_OWN_PREFIX) or name


def _text(args: object) -> str:
    """What a tool was asked as text a person reads: one argument to a line."""
    if args is None:
        return ""
    if isinstance(args, str):
        return args
    if isinstance(args, Mapping):
        return "\n".join(f"{key}: {_value(value)}" for key, value in args.items())
    return _value(args)


def _value(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
