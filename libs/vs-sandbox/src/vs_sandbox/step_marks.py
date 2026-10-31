"""The mark a step's work carries, so that it is found after the process that started it died.

A stage command starts with ``STEP_TAG_ENV=<tag>`` in its environment, which every descendant
inherits (Linux reads it back from ``/proc``), and a Docker candidate's container carries it as a
label (:data:`STEP_TAG_LABEL`). macOS shows no other process's environment, only its arguments,
so the command's process group leader also carries ``STEP_TAG_ENV=<tag>`` as an argument:
:func:`step_shell_argv` starts the command under a leader that stays alive, waits for the shell
that runs it and exits with its status.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

STEP_TAG_ENV = "VIBESYS_STEP_TAG"
"""The environment variable, and the leader argument's name, that say which step a process belongs to."""

STEP_TAG_LABEL = "vibesys.step-tag"
"""The container label that says which step a Docker candidate belongs to."""

_FORBIDDEN = ("\n", "\0", " ", "\t")

# ``$0`` is the mark, ``$1`` the shell and ``$2`` the command. Ending in ``exit`` keeps the shell
# from replacing itself with the command, which would drop the mark from the leader's arguments.
_LEADER_SCRIPT = '"$1" -c "$2"; exit "$?"'


def step_tag_env(tag: str) -> dict[str, str]:
    """The environment that marks a process as started by the step *tag*.

    A tag that is empty or holds whitespace, a newline or NUL cannot be one line of
    ``/proc/<pid>/environ`` or one argument of ``ps`` that a sweep can match exactly, so it is
    refused here rather than marking nothing.
    """
    if not tag or any(char in tag for char in _FORBIDDEN):
        message = f"step tag {tag!r} must be non-empty and hold no whitespace or NUL"
        raise ValueError(message)
    return {STEP_TAG_ENV: tag}


def step_shell_argv(shell: str, command: str, env: Mapping[str, str]) -> tuple[str, ...]:
    """The argv that runs *command* under *shell*, led by a process that carries *env*'s mark.

    Without a mark in *env* it is the plain ``shell -c command``.
    """
    tag = env.get(STEP_TAG_ENV)
    if tag is None:
        return (shell, "-c", command)
    return (shell, "-c", _LEADER_SCRIPT, f"{STEP_TAG_ENV}={tag}", shell, command)
