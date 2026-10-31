"""Every kind of request core can ask for is in the recovery design's table of runners.

The cutover runs every step through a runner, so a new kind of request must say which runner owns
it, or that it has none yet. The table is prose; this is the check that holds it complete.
"""

import re
import typing
from pathlib import Path

from vs_core.api import Request

_DOC = Path(__file__).resolve().parents[2] / "docs/contributing/run-recovery.md"
_HEADING = "### The runner of each request kind"


def _kinds(annotation: object) -> list[str]:
    """The names of the classes a union of request types names, whatever its nesting."""
    value = getattr(annotation, "__value__", annotation)
    origin = typing.get_origin(value)
    if origin is typing.Annotated:
        return _kinds(typing.get_args(value)[0])
    members = typing.get_args(value)
    if members:
        return [name for member in members for name in _kinds(member)]
    if not isinstance(value, type):
        message = f"{value!r} is neither a class nor a union of classes"
        raise TypeError(message)
    return [value.__name__]


def _section() -> str:
    text = _DOC.read_text(encoding="utf-8")
    start = text.index(_HEADING)
    end = text.find("\n### ", start + len(_HEADING))
    return text[start : end if end != -1 else len(text)]


def test_every_request_kind_is_named_in_the_table_of_runners() -> None:
    named = set(re.findall(r"`([A-Za-z]+)`", _section()))
    kinds = _kinds(Request)

    assert len(kinds) == len(set(kinds)) == 24
    assert [kind for kind in kinds if kind not in named] == []
