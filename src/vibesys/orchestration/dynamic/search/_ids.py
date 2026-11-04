"""Keys of dynamic's concurrent flows: the input's measurement, each planning call and workstream.

The key order is the order the search folds finished work in: workstreams in the order they
were scheduled.
"""

from __future__ import annotations

from vibesys.candidates.api import Baseline
from vs_core.api.search import LatchKey, component

PLANNING_PREFIX = "plan."

INPUT_KEY = "input"
"""The key of the one task that measures the input."""

INPUT_MEASURED = LatchKey[Baseline]("input-measured")
"""The input's measurement, set by the root when it lands; workstreams started earlier wait on it."""


def planning_key(call: int) -> str:
    """The key of the ``call``-th planning call (calls are numbered from 1)."""
    return f"{PLANNING_PREFIX}{call}"


def workstream_key(sequence: int, work_id: str) -> str:
    """The key of a workstream: its schedule position first, so keys sort in schedule order."""
    return f"{sequence:06d}.{component(work_id)}"
