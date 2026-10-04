"""Stable identity of an evaluation failure, independent of incidental message text.

Two failures with the same signature raised the same exception type at the same
source line, so they are one defect even when their messages, timings, or
staging paths differ. Only the last traceback in a message counts: a host
message may quote a client's traceback before the server log that holds the
cause.
"""

from __future__ import annotations

import re
from pathlib import PurePath

_TRACEBACK = "Traceback (most recent call last):"
_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)')
_EXCEPTION = re.compile(r"^(?P<type>[A-Za-z_][\w.]*)(?::|$)")


def failure_signature(failure: str) -> str | None:
    """Return ``"<exception type> at <file name>:<line>"`` for the innermost frame.

    The frame is the last ``File "...", line N`` entry of the last Python
    traceback in ``failure``, and the type is the first unindented line after
    it. The file is reduced to its name, because staging moves candidates
    between directories. Returns ``None`` when ``failure`` has no complete
    traceback, so an unrecognized message never matches another.
    """
    start = failure.rfind(_TRACEBACK)
    if start < 0:
        return None
    frame: re.Match[str] | None = None
    for line in failure[start + len(_TRACEBACK) :].splitlines():
        match = _FRAME.match(line)
        if match is not None:
            frame = match
            continue
        if frame is None or not line or line[0].isspace():
            continue
        exception = _EXCEPTION.match(line)
        if exception is None:
            return None
        name = PurePath(frame["file"].replace("\\", "/")).name
        return f"{exception['type']} at {name}:{frame['line']}"
    return None


__all__ = ["failure_signature"]
