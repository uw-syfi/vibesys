"""The frame that proves a read-only scheduler answer arrived whole.

``squeue`` and ``sacct`` legitimately print nothing (no such job, no
accounting row yet), so an empty answer cannot be told from one whose every
byte was lost in transit, and an answer cut exactly at a line break looks
whole. Every read-only scheduler query therefore runs framed: the remote
shell prints :data:`READ_END` on a line of its own only after the query
succeeded, and an answer without that last line is incomplete, which a
caller reports as unknown, never as absent.

Any other writer that finishes its stdout with an end line of its own (the profile
capture's summary) is read with :func:`unframed` under that line, so one rule decides
whether a reply is whole.

Fakes that answer scheduler queries use :func:`split_framed` to find the
query inside a framed command and append :data:`READ_END` to its answer.
"""

from __future__ import annotations

READ_END = "__VS_SCHEDULER_READ_END__"
_SUFFIX = f" && echo {READ_END}"


def framed(command: str) -> str:
    """``command``, followed by the end line once it has succeeded."""
    return command + _SUFFIX


def unframed(stdout: str, end: str = READ_END) -> str | None:
    """The output before its last line ``end``, or None when that line did not arrive whole.

    The rule is the same for any reply that a writer finishes with an end line: a
    scheduler query framed by :func:`framed`, or a capture's summary that prints its
    own. Nothing may follow the end line.
    """
    body, marker, rest = stdout.rpartition(end)
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
