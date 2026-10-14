"""How deep a test session searches: the one setting every sampling test follows.

A pull request samples; the nightly job explores. The pytest plugin sets the depth from
``--depth=pr|nightly``, and no test picks its own: a test states what each depth costs
(``examples(pr=3, nightly=12)``, ``swept(count, sample)``) and the session chooses.
"""

from __future__ import annotations

from contextlib import contextmanager
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


class Depth(StrEnum):
    """The session's search depth."""

    PR = "pr"
    NIGHTLY = "nightly"


class _Session:
    """The one place the session's depth lives."""

    value = Depth.PR


def depth() -> Depth:
    """The depth of this session (`Depth.PR` unless the pytest plugin chose otherwise)."""
    return _Session.value


def set_depth(chosen: Depth) -> None:
    """Choose the depth for the rest of the session (the pytest plugin does, from its option)."""
    _Session.value = chosen


@contextmanager
def using_depth(chosen: Depth) -> Iterator[None]:
    """Follow ``chosen`` inside the block, then return to the previous depth."""
    previous = depth()
    set_depth(chosen)
    try:
        yield
    finally:
        set_depth(previous)


def examples(*, pr: int, nightly: int) -> int:
    """A count (Hypothesis ``max_examples``, steps, seeds) for the session's depth."""
    return nightly if depth() is Depth.NIGHTLY else pr
