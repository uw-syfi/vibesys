"""Liveness of the skeleton under injected faults at the executor boundary.

The cancelled-attempt plan is the one scenario that runs straight through today (the
measured and kept-candidate plans stop at the gaps in ``test_skeleton.py``), so it is the
subject. For every fault schedule Hypothesis generates, the run must: (a) reach a terminal
status with no intent left in flight, within a round bound; (b) raise no core or shell
contract error from its own events; (c) complete every close and discard intent, so each
scope close reached released.

Duplicate delivery is idempotent today. A crash between an effect and its receipt is not
survivable yet: core inspects the dispatched request after the restart, and that is routed to
an executor that cannot answer (gap G7 in the skeleton handoff), so the crash example is a
strict expected failure naming it.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.skeleton_faults import Fault, FaultSchedule, InjectedCrashError
from tests.support.skeleton_strategy import SkeletonStrategy
from tests.support.skeleton_world import (
    LEASE,
    Process,
    World,
    drive,
    open_skeleton_world,
)

from vs_core.api import ContractError, IntentPhase, RunStatus
from vs_runtime.api.core import (
    ObservationRejectedError,
    OwnerEventRejectedError,
    ReceiptCorruptError,
)

INTERNAL_ERRORS = (ContractError, ObservationRejectedError, OwnerEventRejectedError)
ENDED = {IntentPhase.COMPLETED, IntentPhase.BLOCKED}


async def _run(root: Path, schedule: FaultSchedule) -> Process:
    """Start the run and restart it after each injected crash, until it is terminal."""
    with open_skeleton_world(root, SkeletonStrategy.cancelled(), schedule) as world:
        now = 0.0
        for _ in range(len(schedule.at) + 1):
            process = world.runtime()
            process.shell.start(f"host-{schedule.crashes}", now_at=now, lease_duration=LEASE)
            try:
                refusal = await drive(process, start=now + 1.0)
            except InjectedCrashError:
                now += LEASE + 2.0
                continue
            assert refusal is None, refusal
            _assert_live(process, world)
            return process
    message = "the run kept crashing"
    raise AssertionError(message)


def _assert_live(process: Process, world: World) -> None:
    del world
    core = process.shell.record.envelope.core
    assert core.run.status == RunStatus.TERMINAL
    in_flight = [i.request.kind for i in core.intents.intents if i.phase not in ENDED]
    assert not in_flight, f"intents not ended: {in_flight}"
    closes = [
        i
        for i in core.intents.intents
        if "close" in i.request.kind.lower() or "discard" in i.request.kind.lower()
    ]
    assert all(i.phase == IntentPhase.COMPLETED for i in closes), "a scope close did not complete"


def _run_sync(schedule: FaultSchedule) -> Process:
    with tempfile.TemporaryDirectory() as tmp:
        return asyncio.run(_run(Path(tmp), schedule))


@settings(
    max_examples=12,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(at=st.dictionaries(st.integers(0, 11), st.just(Fault.DUPLICATE), max_size=4))
def test_duplicate_delivery_never_stalls_or_breaks_the_run(at: dict[int, Fault]) -> None:
    try:
        process = _run_sync(FaultSchedule(at))
    except INTERNAL_ERRORS as error:  # property (b): never from the run's own events
        message = f"internal event rejected under {at}: {error}"
        raise AssertionError(message) from error
    assert process.shell.record.envelope.core.run.result is not None


@pytest.mark.xfail(
    raises=(AssertionError, ReceiptCorruptError, *INTERNAL_ERRORS),
    strict=True,
    reason=(
        "a crash between an effect and its receipt cannot be recovered: core inspects the "
        "dispatched request after the restart, InspectRequest is routed to the OPERATIONS "
        "executor (_core_requests.py:215) which knows only operation receipts "
        "(_operation_requests.py:326, _operation_receipts.py:95); gap G7, owner OPS-OWNERS"
    ),
)
def test_a_crash_after_the_first_effect_is_recovered() -> None:
    _run_sync(FaultSchedule({0: Fault.CRASH_AFTER_EFFECT}))
