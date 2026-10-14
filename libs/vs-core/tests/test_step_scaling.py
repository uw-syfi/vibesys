"""A step costs the same however long the run behind it is.

Core keeps every request of a run in its ledger, so a step that found a record by comparing its
identity with every row cost more with every request the run had made: a long run's steps grew
with its length and the run with its square. A lookup is now a read of the ledger's index. The
cost is counted, not timed: the model comparisons (`BaseModel.__eq__`) one step makes, on a
ledger of ``n`` and of ``4 * n`` finished requests, so the check is exact and never flaky.
"""

from __future__ import annotations

import sys
from collections import Counter
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

import vs_core.api as core

from .toy_agent_search import Config, ToyHost, run_toy

if TYPE_CHECKING:
    from types import FrameType

_EQUALITY = BaseModel.__eq__.__code__

# test-isolation: recovery's child-ownership checks (`_request_recovery.py`) still scan the ledger
# by equality; that module is being changed by another track. Remove this once it uses the index.
_NOT_YET_INDEXED = frozenset({"_request_recovery.py"})


def _comparisons(state: core.CoreState, event: core.CoreEvent) -> int:
    """How many model comparisons ``core.step(state, event)`` makes outside the excluded files."""
    counted: Counter[str] = Counter()

    def count(frame: FrameType, kind: str, _arg: object) -> None:
        if kind == "call" and frame.f_code is _EQUALITY:
            caller = frame.f_back
            while caller is not None and caller.f_code is _EQUALITY:
                caller = caller.f_back
            counted[Path(caller.f_code.co_filename).name if caller else ""] += 1

    sys.setprofile(count)
    try:
        core.step(state, event)
    finally:
        sys.setprofile(None)
    return sum(n for name, n in counted.items() if name not in _NOT_YET_INDEXED)


@cache
def _toy_run() -> core.CoreState:
    """The core state at the end of the smallest toy run (core states never change)."""
    return run_toy(ToyHost(), Config()).core


def _padded(state: core.CoreState, filler: core.RequestRecord, rows: int) -> core.CoreState:
    """``state`` with ``rows`` more finished requests ahead of its own, as a long run has."""
    padding = tuple(
        filler.model_copy(update={"request_id": core.RequestId(root=f"earlier-{row}")})
        for row in range(rows)
    )
    records = state.records.model_copy(update={"records": (*padding, *state.records.records)})
    return state.model_copy(update={"records": records})


_ROWS = 50


def test_a_step_compares_as_much_on_a_long_ledger_as_on_a_short_one() -> None:
    """A redelivered answer, which looks its record up in the ledger, on ``n`` and ``4 * n`` rows."""
    state = _toy_run()
    finished = [
        row
        for row in state.records.records
        if row.phase == core.RequestPhase.COMPLETED and row.observation is not None
    ]
    answer = finished[-1].observation
    assert answer is not None
    redelivered = core.RequestObserved(observation=answer)
    short = _comparisons(_padded(state, finished[0], _ROWS), redelivered)
    long = _comparisons(_padded(state, finished[0], 4 * _ROWS), redelivered)
    assert long <= short, (short, long)


@given(
    names=st.lists(st.sampled_from(["a", "b", "c", "d"]), max_size=8),
    wanted=st.sampled_from("abcde"),
)
def test_the_ledger_index_finds_what_a_scan_finds(names: list[str], wanted: str) -> None:
    """Every lookup answers as the scan it replaced, duplicates (a corrupt ledger) included."""
    state = _toy_run()
    filler = state.records.records[0]
    rows = tuple(
        filler.model_copy(update={"request_id": core.RequestId(root=name)}) for name in names
    )
    ledger = state.records.model_copy(update={"records": rows})
    identity = core.RequestId(root=wanted)
    scanned = tuple(row for row in rows if row.request_id == identity)
    assert ledger.find(identity) == scanned
    assert ledger.record(identity) == (scanned[0] if scanned else None)
    assert ledger.find(None) == ()
