"""Count the protobuf encodes and decodes a piece of work makes, and the bytes they move.

A cost that grows with a run is almost always a decode or an encode of something the run
accumulated, so counting them says how much work was done without timing it: the count is exact,
the same on every machine, and never flaky. The counter is a ``sys.monitoring`` observer, so it
changes nothing it measures.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_CODECS = frozenset({"from_binary", "to_binary", "from_json", "to_json"})
_TOOL_IDS = (3, 4)


@dataclass(frozen=True)
class CodecWork:
    """How many codec calls were made, and the bytes of message they decoded or encoded."""

    calls: int
    size: int


def codec_work[T](work: Callable[[], T]) -> tuple[T, CodecWork]:
    """Run ``work``; return its result and the protobuf codec calls it made."""
    monitoring = sys.monitoring
    tool = next(t for t in _TOOL_IDS if monitoring.get_tool(t) is None)
    calls = [0, 0]

    def size(encoded: object) -> int:
        return len(encoded) if isinstance(encoded, bytes | str) else 0

    def observe(_code: object, _offset: int, function: object, argument: object) -> None:
        name = getattr(function, "__name__", "")
        if name not in _CODECS:
            return
        calls[0] += 1
        if name in {"from_binary", "from_json"}:
            calls[1] += size(argument)
        else:
            # The instance is the bound function's own, or the first argument of a method.
            owner = getattr(function, "__self__", argument)
            calls[1] += size(getattr(owner, name)())

    monitoring.use_tool_id(tool, "codec-work")
    try:
        monitoring.register_callback(tool, monitoring.events.CALL, observe)
        monitoring.set_events(tool, monitoring.events.CALL)
        result = work()
    finally:
        monitoring.set_events(tool, 0)
        monitoring.register_callback(tool, monitoring.events.CALL, None)
        monitoring.free_tool_id(tool)
    return result, CodecWork(calls[0], calls[1])
