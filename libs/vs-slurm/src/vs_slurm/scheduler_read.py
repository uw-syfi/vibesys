"""The frame that proves a read-only scheduler answer arrived whole.

``squeue`` and ``sacct`` legitimately print nothing (no such job, no
accounting row yet), so an empty answer cannot be told from one whose every
byte was lost in transit, and an answer cut exactly at a line break looks
whole. Every read-only scheduler query therefore runs framed: the remote
shell prints :data:`READ_END` on a line of its own only after the query
succeeded, and an answer without that last line is incomplete, which a
caller reports as unknown, never as absent.

Fakes that answer scheduler queries use :func:`split_framed` to find the
query inside a framed command and append :data:`READ_END` to its answer.
"""

from __future__ import annotations

READ_END = "__VS_SCHEDULER_READ_END__"
_SUFFIX = f" && echo {READ_END}"


def framed(command: str) -> str:
    """``command``, followed by the end line once it has succeeded."""
    return command + _SUFFIX


def unframed(stdout: str) -> str | None:
    """The query's own output, or None when the end line did not arrive whole."""
    body, marker, rest = stdout.rpartition(READ_END)
    if not marker or rest != "\n" or (body and not body.endswith("\n")):
        return None
    return body


def split_framed(command: str) -> tuple[str, str]:
    """The query inside ``command`` and what its answer must end with.

    A command that is not framed is returned unchanged, with nothing to append.
    """
    if command.endswith(_SUFFIX):
        return command.removesuffix(_SUFFIX), READ_END + "\n"
    return command, ""
