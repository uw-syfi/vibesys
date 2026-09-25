"""Translate semantic paths in evaluator command arguments safely."""

from __future__ import annotations

import json
from pathlib import Path

_SHELL_COMMAND_ARG_COUNT = 3


def _translate_command_argument(
    argument: str,
    replacements: list[tuple[str, str]],
) -> str:
    """Translate paths in one argv item, including serialized nested argv."""
    if not any(source in argument for source, _ in replacements):
        return argument
    try:
        nested = json.loads(argument)
    except json.JSONDecodeError:
        nested = None
    if isinstance(nested, list) and all(isinstance(item, str) for item in nested):
        _reject_semantic_tokens_in_source(nested, replacements)
        translated: list[str] = []
        for item in nested:
            translated_item = item
            for source, destination in replacements:
                translated_item = translated_item.replace(source, destination)
            translated.append(translated_item)
        return json.dumps(translated, separators=(",", ":"))
    for source, destination in replacements:
        argument = argument.replace(source, destination)
    return argument


def _reject_semantic_tokens_in_source(
    arguments: list[str],
    replacements: list[tuple[str, str]],
) -> None:
    """Reject raw path substitution into shell or interpreter source code."""
    source_index = _executable_source_index(arguments)
    if source_index is not None and any(
        source in arguments[source_index] for source, _ in replacements
    ):
        message = (
            "semantic path tokens in executable source are unsafe; pass them as "
            "positional arguments after the source"
        )
        raise ValueError(message)


def _executable_source_index(arguments: list[str]) -> int | None:
    """Return the source-code argv index for supported command interpreters."""
    shell_source_index = _nested_shell_source_index(arguments)
    if shell_source_index is not None:
        return shell_source_index
    command_index, split_string_index = _env_wrapped_command_indexes(arguments)
    if split_string_index is not None or command_index is None:
        return split_string_index
    executable = Path(arguments[command_index]).name
    if executable in {"node", "nodejs"}:
        return _option_value_index(
            arguments,
            command_index,
            separate={"-e", "-p", "--eval", "--print"},
            inline_prefixes=("--eval=", "--print="),
        )
    if _is_python_executable(executable):
        return _option_value_index(arguments, command_index, separate={"-c"})
    return None


def _option_value_index(
    arguments: list[str],
    command_index: int,
    *,
    separate: set[str],
    inline_prefixes: tuple[str, ...] = (),
) -> int | None:
    for index, argument in enumerate(arguments[command_index + 1 :], start=command_index + 1):
        if argument in separate:
            source_index = index + 1
            return source_index if source_index < len(arguments) else None
        if inline_prefixes and argument.startswith(inline_prefixes):
            return index
    return None


def _is_python_executable(executable: str) -> bool:
    suffix = executable.removeprefix("python")
    return executable.startswith("python") and (
        not suffix or all(part.isdigit() for part in suffix.split("."))
    )


def _nested_shell_source_index(arguments: list[str]) -> int | None:
    """Return the script index for common ``sh`` and ``bash`` command forms."""
    command_index, split_string_index = _env_wrapped_command_indexes(arguments)
    if split_string_index is not None:
        return split_string_index
    if command_index is None:
        return None
    arguments = arguments[command_index:]
    if len(arguments) < _SHELL_COMMAND_ARG_COUNT or arguments[0] not in {
        "bash",
        "sh",
        "/bin/bash",
        "/bin/sh",
        "/usr/bin/bash",
        "/usr/bin/sh",
    }:
        return None
    for index, argument in enumerate(arguments[1:], start=1):
        if argument == "--":
            return None
        if argument == "-c" or (
            argument.startswith("-") and not argument.startswith("--") and "c" in argument[1:]
        ):
            source_index = index + 1
            return command_index + source_index if source_index < len(arguments) else None
    return None


def _env_wrapped_command_indexes(arguments: list[str]) -> tuple[int | None, int | None]:
    """Return command and shell-like split-string indexes for an ``env`` wrapper."""
    if not arguments or arguments[0] not in {"env", "/bin/env", "/usr/bin/env"}:
        return 0, None
    index = 1
    while index < len(arguments):
        action, width = _classify_env_argument(arguments[index])
        if action == "command":
            return index, None
        if action == "end-options":
            index += width
            return (index if index < len(arguments) else None), None
        if index + width > len(arguments):
            return None, None
        if action == "split-string":
            return None, index + width - 1
        index += width
    return None, None


def _classify_env_argument(argument: str) -> tuple[str, int]:
    """Classify one GNU ``env`` wrapper argument and its argv width."""
    action = "command"
    width = 0
    if argument == "--":
        action, width = "end-options", 1
    elif argument in {"-S", "--split-string"}:
        action, width = "split-string", 2
    elif argument.startswith("--split-string=") or (argument.startswith("-S") and argument != "-S"):
        action, width = "split-string", 1
    elif argument in {"-C", "-u", "--argv0", "--chdir", "--unset"}:
        action, width = "option", 2
    elif argument.startswith("-"):
        action, width = "option", 1
    elif "=" in argument and argument.partition("=")[0]:
        action, width = "assignment", 1
    return action, width
