"""Contract of the host-boundary gate: it counts every crossing and crashes only where planned."""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_faults.api import Boundary, FaultGate, FaultPlan, FaultRule, HostCrashError, HostFault

BOUNDARIES = st.sampled_from([Boundary.EXECUTOR_REQUEST, Boundary.DURABLE_WRITE])
TARGETS = st.sampled_from(["a", "b", "c"])


def _crash_rule(boundary: Boundary, target: str, at: int) -> FaultRule:
    return FaultRule(boundary=boundary, target=target, at=at, fault=HostFault.CRASH_AFTER)


@given(calls=st.lists(st.tuples(BOUNDARIES, TARGETS), max_size=30))
def test_an_empty_plan_passes_every_call_through_and_records_it(
    calls: list[tuple[Boundary, str]],
) -> None:
    gate = FaultGate(FaultPlan(seed=0))
    for boundary, target in calls:
        assert gate.around(boundary, target, lambda: 7) == 7
    assert [(c.boundary, c.target) for c in gate.calls] == calls


@given(
    calls=st.lists(st.tuples(BOUNDARIES, TARGETS), min_size=1, max_size=30),
    data=st.data(),
)
def test_a_crash_follows_the_effect_at_exactly_the_planned_ordinal(
    calls: list[tuple[Boundary, str]], data: st.DataObject
) -> None:
    recorded = FaultGate(FaultPlan(seed=0))
    for boundary, target in calls:
        recorded.around(boundary, target, lambda: None)
    chosen = data.draw(st.sampled_from(recorded.calls))
    rule = _crash_rule(chosen.boundary, chosen.target, chosen.ordinal)
    gate = FaultGate(FaultPlan(seed=1, rules=(rule,)))
    effects: list[int] = []
    crashed_at = None
    for index, (boundary, target) in enumerate(calls):
        try:
            gate.around(boundary, target, lambda index=index: effects.append(index))
        except HostCrashError as crash:
            crashed_at = (crash.boundary, crash.target, crash.ordinal)
            assert effects[-1] == index  # the effect ran before the host died
            break
    assert crashed_at == (chosen.boundary, chosen.target, chosen.ordinal)


def test_heal_stops_injection_and_async_calls_crash_after_their_effect() -> None:
    rule = _crash_rule(Boundary.EXECUTOR_REQUEST, "a", 1)
    gate = FaultGate(FaultPlan(seed=0, rules=(rule,)))
    ran: list[str] = []

    async def effect() -> None:
        ran.append("a")

    with pytest.raises(HostCrashError):
        asyncio.run(gate.around_async(Boundary.EXECUTOR_REQUEST, "a", effect))
    assert ran == ["a"]
    gate.heal()
    asyncio.run(gate.around_async(Boundary.EXECUTOR_REQUEST, "a", effect))
    assert ran == ["a", "a"]


def test_a_rule_for_a_non_host_boundary_is_not_a_host_fault() -> None:
    with pytest.raises(ValueError, match="not a host boundary"):
        FaultGate(FaultPlan(seed=0)).around(Boundary.CLUSTER, "x", lambda: None)
