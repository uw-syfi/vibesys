"""Core's durable state stays bounded however many polls a run's jobs take.

Core polls each running evaluation job until it ends, and each poll is a request in the ledger.
A ten-hour run held 6,722 finished polls, 15 MB of its 28 MB store (FINDINGS S171), and the
store is rewritten on every commit, so commit cost grew with run length. A finished poll whose
answer core has taken in is spent: nothing reads it again, and its job names the next poll by
the observation it follows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from .toy_agent_search import Config, ToyHost, run_toy

if TYPE_CHECKING:
    import vs_core.api as core

_POLLS = 10


def _ledger_after(polls: int) -> core.CoreState:
    """The end state of a toy run whose one evaluation job reports running ``polls`` times."""
    trace = run_toy(ToyHost(waits=(True,), polls=(polls,)), Config())
    assert trace.finished
    return trace.core


def _encoded(state: core.CoreState) -> int:
    """The bytes of what grows with a run's requests: the ledger, recovery, and receipts."""
    return len(state.records.model_dump_json()) + len(state.run.model_dump_json())


@pytest.mark.xfail(strict=True, reason="FINDINGS S171: finished job polls are never pruned")
def test_the_ledger_holds_as_many_records_after_four_times_the_polls() -> None:
    short = _ledger_after(_POLLS)
    long = _ledger_after(4 * _POLLS)
    assert len(long.records.records) == len(short.records.records)
    # Only the sequence numbers inside the kept ids and times may grow by a few digits.
    assert _encoded(long) <= _encoded(short) + 256
