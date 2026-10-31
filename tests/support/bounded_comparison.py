"""Keep the explanation of a failed ``==`` short, however large the operands.

When ``assert actual == expected`` fails, pytest explains it with a line diff of the operands'
pretty-printed forms. That diff is quadratic in the operand size, and it is on whenever the run is
verbose or on CI. Two stale 300 KB replay goldens took 223 s and 803 s to explain, so the 10 minute
cancel of a CI shard fired and the shard looked hung instead of failed.

This plugin answers first for a comparison with a large operand: the operands' sizes and digests,
and the first place they differ. A comparison of small operands is left to pytest, whose diff is
readable there.
"""

from __future__ import annotations

import hashlib
import os

#: Operands (or their ``repr``) up to this many characters keep pytest's own diff.
LARGE_OPERAND = 8_000
#: Characters shown on each side of the first difference.
CONTEXT = 48


def pytest_assertrepr_compare(op: str, left: object, right: object) -> list[str] | None:
    """Pytest hook: a short explanation for a failed ``==`` over a large operand."""
    return explain_comparison(op, left, right)


def explain_comparison(op: str, left: object, right: object) -> list[str] | None:
    """The lines explaining why ``left == right`` failed, or None when pytest should explain it.

    The first line is the summary pytest prints after the assertion. None is returned for any
    operator but ``==`` and for operands small enough for pytest's line diff.
    """
    if op != "==":
        return None
    forms = _comparable_forms(left, right)
    if max(len(form) for form in forms) <= LARGE_OPERAND:
        return None
    return describe_difference(*forms, names=(type(left).__name__, type(right).__name__))


def describe_difference(
    left: str | bytes, right: str | bytes, *, names: tuple[str, str] = ("left", "right")
) -> list[str]:
    """Sizes, digests and the first difference of two values of one type, in a few short lines."""
    summary = (
        f"{names[0]} == {names[1]}: operands of {len(left)} and {len(right)} characters, "
        "so only their first difference is shown"
    )
    lines = [summary, f"  sha256 {_digest(left)} against {_digest(right)}"]
    offset = len(os.path.commonprefix([left, right]))
    if left == right:
        lines.append("  the printed forms are identical; the objects differ in a value they hide")
        return lines
    lines.append(f"  first difference at offset {offset}")
    for side, value in (("left ", left), ("right", right)):
        window = value[max(0, offset - CONTEXT) : offset + CONTEXT]
        lines.append(f"  {side}: {'...' if offset > CONTEXT else ''}{window!r}")
    return lines


def _comparable_forms(left: object, right: object) -> tuple[str | bytes, str | bytes]:
    """Both operands as text, or both as bytes."""
    if isinstance(left, str) and isinstance(right, str):
        return left, right
    if isinstance(left, bytes) and isinstance(right, bytes):
        return left, right
    return repr(left), repr(right)


def _digest(value: str | bytes) -> str:
    data = value if isinstance(value, bytes) else value.encode("utf-8", errors="backslashreplace")
    return hashlib.sha256(data).hexdigest()[:12]
