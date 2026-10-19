"""Core's durable state stays bounded however many polls a run's jobs take.

Core polls each running evaluation job until it ends, and each poll is a request in the ledger.
A ten-hour run held 6,722 finished polls, 15 MB of its 28 MB store (FINDINGS S171), and the
store is rewritten on every commit, so commit cost grew with run length. A finished poll whose
answer core has taken in is spent: nothing reads it again, and its job names the next poll by
the observation it follows. Pruning spent polls must change nothing a run does: the same
requests, observations, decisions and effects, in the same order, with the same ids.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.testing import Faults, drive
from vs_sim.api.testing import examples

from .test_agent_turn_runs import SCENARIOS, Scenario
from .toy_agent_search import OPERATIONS, Config, ToyHost, run_toy, toy, toy_harness

if TYPE_CHECKING:
    from vs_core.api.testing import Trace

_POLLS = 10
_UNPRUNED = core.CoreReducers(ledger=lambda state: state)
_SETTINGS = settings(max_examples=examples(pr=4, nightly=300), deadline=None, database=None)


def _unpruned(host: ToyHost, config: Config, faults: Faults | None = None) -> Trace:
    """The toy run of ``run_toy`` on a core whose ledger keeps every request."""
    harness = toy_harness(host, reducers=_UNPRUNED)
    return drive(toy.driver(config, OPERATIONS.registry()), config, host, harness, faults)


def _ledger_after(polls: int, *, pruned: bool = True) -> Trace:
    """A finished toy run whose one evaluation job reports running ``polls`` times."""
    host = ToyHost(waits=(True,), polls=(polls,))
    trace = run_toy(host, Config()) if pruned else _unpruned(host, Config())
    assert trace.finished
    return trace


def _encoded(state: core.CoreState) -> int:
    """The bytes of what grows with a run's requests: the ledger, recovery, and receipts."""
    return len(state.records.model_dump_json()) + len(state.run.model_dump_json())


def _observable(trace: Trace) -> tuple[object, ...]:
    """Everything a run did that anything outside core sees, in order and with its ids."""
    return (
        trace.finished,
        trace.events,
        trace.requests,
        trace.log,
        trace.decisions,
        trace.discarded,
        trace.effects,
        trace.state,
    )


def test_the_ledger_holds_as_many_records_after_four_times_the_polls() -> None:
    short = _ledger_after(_POLLS).core
    long = _ledger_after(4 * _POLLS).core
    assert len(long.records.records) == len(short.records.records)
    # Only the sequence numbers inside the kept ids and times may grow by a few digits.
    assert _encoded(long) <= _encoded(short) + 256


def test_without_pruning_the_ledger_grows_with_every_poll() -> None:
    """The control for the bound above: the same runs keep every poll when nothing prunes."""
    short = _ledger_after(_POLLS, pruned=False).core
    long = _ledger_after(4 * _POLLS, pruned=False).core
    assert len(long.records.records) >= len(short.records.records) + 3 * _POLLS


def _host(scenario: Scenario) -> ToyHost:
    return ToyHost(
        waits=scenario.waits,
        polls=scenario.polls,
        roles=scenario.roles,
        ends=scenario.ends,
        shares=scenario.shares,
    )


_PRUNING_FAULTS = st.builds(
    Faults,
    duplicate=st.booleans(),
    reorder=st.booleans(),
    reload=st.booleans(),
    crash_after=st.frozensets(st.integers(1, 40), max_size=1),
    reobserve_after=st.frozensets(st.integers(1, 40), max_size=1),
)


@_SETTINGS
@given(scenario=SCENARIOS, faults=_PRUNING_FAULTS)
def test_pruning_changes_nothing_a_run_does(scenario: Scenario, faults: Faults) -> None:
    """Same events, requests, observations, decisions and effects, with or without pruning.

    Duplicated, reordered and repeated observations reach spent polls, and crashes restart
    the run from a pruned envelope, so every way an answer can arrive after its poll is gone
    is exercised.
    """
    config = Config(workstreams=scenario.workstreams, in_flight=scenario.in_flight)
    # Each run gets its own host: a host counts the jobs it started.
    pruned = run_toy(_host(scenario), config, faults=faults)
    unpruned = _unpruned(_host(scenario), config, faults)
    assert _observable(pruned) == _observable(unpruned)
    # Pruning is a projection of the unpruned state: compacting it gives the pruned ledger.
    assert core.compact_ledger(unpruned.core).records == pruned.core.records
    assert len(pruned.core.records.records) <= len(unpruned.core.records.records)


@_SETTINGS
@given(polls=st.integers(2, 12))
def test_compacting_an_unpruned_ledger_reaches_the_pruned_one_and_stays(polls: int) -> None:
    """A ledger written before pruning existed compacts to the pruned run's, idempotently."""
    unpruned = _ledger_after(polls, pruned=False).core
    once = core.compact_ledger(unpruned)
    assert core.compact_ledger(once) == once
    assert len(once.records.records) == len(_ledger_after(polls).core.records.records)
