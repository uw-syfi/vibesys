"""One scenario through the real core, shell and executors, with every gap named.

The run is built the way a launch will build it: real ``vs_core`` state and step,
the real vs-runtime shell with durable state under a temporary Project, the real
workspace executor on Git and the real evaluation executor on the Fake Slurm
cluster. It plays: baseline measured, one attempt (implementer turn, snapshot,
evaluate), settle, adopt, stop with the result. Two more scenarios repeat it with
a shell crash and restart, after a dispatch and after an observation.

Where a piece does not exist yet, the scenario fails at that interface and is
marked as a strict expected failure naming the missing piece and its owner. When
the owner lands, the marker turns into a failure ("unexpectedly passed") and the
merger removes it. The gap table is in the skeleton handoff.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.support.skeleton_world import (
    CrashPoint,
    Process,
    World,
    drive,
    finished,
    open_skeleton_world,
    run_until_crash,
)

from vs_core.api import RunStatus
from vs_runtime.api.core import CoreContractGapError

if TYPE_CHECKING:
    from pathlib import Path

LEASE = 100.0

# The first gap every scenario meets, in the order the run reaches it.
INTENT_LEDGER = pytest.mark.xfail(
    raises=CoreContractGapError,
    strict=True,
    reason=(
        "vs-core intent ledger is a stub (_intent_ledger.py: dispatch_authorized raises "
        "KernelNotImplementedError); owner #1319 feat/core-intent-ledger"
    ),
)


def _start(world: World, host: str, now: float) -> Process:
    process = world.runtime()
    process.shell.start(host, now_at=now, lease_duration=LEASE)
    return process


def _assert_adopted(process: Process, world: World) -> None:
    core = process.shell.record.envelope.core
    assert finished(process)
    assert core.run.status == RunStatus.TERMINAL
    assert core.run.result is not None
    assert core.run.result.outcome == "success"
    assert core.settlement.adoption is not None
    assert core.settlement.adoption.verified
    # Two measurements (baseline, candidate), each submitted to the cluster exactly once.
    assert len(world.cluster.submissions) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "crash",
    [
        pytest.param(None, id="straight-through", marks=INTENT_LEDGER),
        pytest.param(CrashPoint.AFTER_DISPATCH, id="crash-after-dispatch", marks=INTENT_LEDGER),
        pytest.param(
            CrashPoint.AFTER_OBSERVATION, id="crash-after-observation", marks=INTENT_LEDGER
        ),
    ],
)
async def test_skeleton(tmp_path: Path, crash: CrashPoint | None) -> None:
    with open_skeleton_world(tmp_path) as world:
        process = _start(world, "host-a", 0.0)
        now = 1.0
        if crash is not None:
            now = await run_until_crash(process, crash, start=now)
            process = _start(world, "host-b", now + LEASE + 1.0)
            now += LEASE + 2.0
        assert await drive(process, start=now) is None
        _assert_adopted(process, world)
