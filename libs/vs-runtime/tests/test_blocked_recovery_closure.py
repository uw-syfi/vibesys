"""A run whose recovery is blocked and whose closure waits on an unprovable release halts loudly.

The loop stops a run whose recovery cannot reopen with a ``blocked`` result. If closure then
still needs a release core cannot prove (an owned job's submission completed, and the
inspection that would tell whether the job ended was never answered), nothing can end it.
Before, the loop kept polling the blocked recovery until the run deadline, then reported that
"the strategy has stopped proposing". Now it halts as soon as the stop is recorded, and names
the record whose release is unproven.
"""

from __future__ import annotations

import json
from hashlib import sha256

import pytest

from vs_core.api import (
    Access,
    CoreState,
    EnsureSession,
    EventId,
    LifecycleClass,
    Observation,
    ObservationStatus,
    RecordsState,
    RecoveryBarrier,
    RecoveryCheck,
    RecoveryPhase,
    RequestId,
    RequestPhase,
    RequestRecord,
    RoleId,
    RunStatus,
    Scope,
    SessionId,
    SessionSpec,
    Until,
    initial_state,
)
from vs_project.api import FakeStateStore
from vs_runtime.api.core import (
    CoreRunHost,
    CoreRuntime,
    RunLoopConfig,
    RunStalledError,
    drive_core,
    start_core,
)
from vs_runtime.api.core_shell_fixtures import CounterConfig, counter
from vs_runtime.api.testing import FakePublicationDelivery
from vs_sim.api.testing import VirtualClock, run_virtual


def _unprovable_release() -> CoreState:
    """A run after a restart: one owned job submitted, and recovery blocked on it."""
    core = initial_state()
    request_id = RequestId(root="submission")
    request = EnsureSession(
        request_id=request_id,
        scope=Scope(owner=core.run.run_id, generation=0),
        spec=SessionSpec(
            session_id=SessionId(root="job"),
            role_id=RoleId(root="worker"),
            policy="reuse",
            lifetime="owner",
            access=Access.WRITE_ARTIFACTS,
        ),
    )
    accepted = Observation(
        event_id=EventId(root="accepted"),
        request_id=request_id,
        scope=request.scope,
        sequence=1,
        observed_at=0.0,
        status=ObservationStatus.SUCCEEDED,
        accepted=True,
        terminal=True,
        released=False,
    )
    record = RequestRecord(
        request_id=request_id,
        request=request,
        payload_digest=sha256(
            json.dumps(
                request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
        lifecycle=LifecycleClass.OWNED_JOB,
        phase=RequestPhase.COMPLETED,
        until=Until(at=100.0),
        addressed_by=(),
        observation=accepted,
    )
    barrier = RecoveryBarrier(
        epoch=1,
        phase=RecoveryPhase.BLOCKED,
        checks=(
            RecoveryCheck(target=request_id, inspection=None, resolution="blocked", wait=None),
        ),
    )
    return core.model_copy(
        update={
            "run": core.run.model_copy(update={"declaration": counter.declaration}),
            "records": RecordsState(hosts=(), records=(record,), recovery=barrier),
        }
    )


def test_a_blocked_recovery_whose_closure_needs_an_unprovable_release_halts_at_once() -> None:
    state = _unprovable_release()
    shell = CoreRuntime(
        FakeStateStore(),
        counter.driver(CounterConfig()),
        state,
        config=CounterConfig(),
    )
    clock = VirtualClock(1.0)
    host = CoreRunHost(shell, FakePublicationDelivery(), clock)
    config = RunLoopConfig(host_id="blocked", lease_duration=100.0, recovery_poll_interval=50.0)
    start_core(host, config)
    with pytest.raises(RunStalledError, match=r"release is unproven \[.*submission'?\]"):
        run_virtual(clock, drive_core(host, config))
    core = shell.record.envelope.core
    assert core.run.status == RunStatus.CLOSING
    assert core.run.result is not None
    assert core.run.result.outcome == "blocked"
    assert clock.now() < state.run.deadline_at
