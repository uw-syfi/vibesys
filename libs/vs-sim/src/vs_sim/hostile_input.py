"""One corpus of hostile JSON frames, shared by every decoder's contract test.

A decoder of bytes from a peer (another process, an agent, an evaluator, a
file another run may have torn) must answer every case here with its value or
its own typed rejection. The repository's rule for such input: parse it with
pydantic's JSON parser, which bounds nesting and reports deep nesting and
invalid UTF-8 as ``ValueError``. ``json.loads`` recurses per level and raises
``RecursionError``, and decoding bytes first raises ``UnicodeDecodeError``;
both escaped handlers that caught only ``JSONDecodeError``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

DEEP_NESTING = 100_000
"""Far past CPython's recursion limit and pydantic's nesting bound."""

OVERSIZED_BYTES = 1024 * 1024 + 1
"""One byte past the largest line a transport of this repository accepts."""


def hostile_json_frames(valid: Sequence[bytes] = ()) -> tuple[tuple[str, bytes], ...]:
    """Named hostile frames, plus every proper prefix of each ``valid`` frame.

    A prefix is a peer that hung up mid-frame. Names are stable, so a failing
    case names itself in a test id.
    """
    cases: list[tuple[str, bytes]] = [
        ("deep-array", b"[" * DEEP_NESTING),
        ("deep-object", b'{"a":' * DEEP_NESTING),
        ("deep-closed", b"[" * DEEP_NESTING + b"]" * DEEP_NESTING),
        ("deep-under-a-key", b'{"type":"x","kind":"x","v":' + b"[" * DEEP_NESTING),
        ("oversized-line", b'{"type":"' + b"x" * OVERSIZED_BYTES + b'"}'),
        ("invalid-utf8", b"\xff\xfe"),
        ("invalid-utf8-in-a-string", b'{"type":"\xff"}'),
        ("lone-surrogate", b'"\\ud800"'),
        ("unknown-keys", json.dumps({"type": "x", "kind": "x", "unknown": [1, {}]}).encode()),
        ("null", b"null"),
        ("number", b"1e999"),
        ("nan", b"NaN"),
        ("string", b'"s"'),
        ("array", b"[]"),
        ("empty", b""),
        ("whitespace", b" \n"),
        ("garbage", b"\x00{]"),
    ]
    for index, frame in enumerate(valid):
        cases.extend((f"valid{index}-cut{cut}", frame[:cut]) for cut in range(len(frame)))
    return tuple(cases)


__all__ = ["DEEP_NESTING", "OVERSIZED_BYTES", "hostile_json_frames"]
