"""A turn that ended waiting is waiting for its checkpoint, and says so while it does."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api import initial_state
from vs_sim.api.testing import examples

from .records_fixtures import fixture_digest, pending_record
from .test_pending_suspension_contract import _invocation, _pending

OWNER = core.AttemptId(root="attempt")


def _yielded(*named: str) -> core.Invocation:
    """A yielded turn; *named* are the requests its recorded wait names."""
    invocation = _invocation(0, OWNER)
    wait = (
        core.WaitOn(requests=tuple(core.RequestId(root=name) for name in named)) if named else None
    )
    return core.Invocation.model_validate(
        {
            **invocation.model_dump(),
            "pending_suspension": _pending(invocation, ["job"], 50.0),
            "wait": None if wait is None else wait.model_dump(),
        }
    )


def _checkpoint(invocation: core.Invocation, phase: core.RequestPhase) -> core.RequestRecord:
    request = core.SnapshotAndRetain(
        request_id=core.RequestId(root="checkpoint"),
        scope=invocation.scope,
        attempt=core.AttemptRef(attempt_id=OWNER, generation=0),
        retention="wip",
        invocation=invocation.invocation,
    )
    return pending_record().model_copy(
        update={
            "request_id": request.request_id,
            "request": request,
            "payload_digest": fixture_digest(request),
            "phase": phase,
        }
    )


def _state(invocation: core.Invocation, *records: core.RequestRecord) -> core.CoreState:
    state = initial_state()
    return state.model_copy(
        update={
            "sessions": core.SessionsState(invocations=(invocation,)),
            "records": state.records.model_copy(update={"records": records}),
        }
    )


def test_a_yield_nothing_checkpoints_is_a_wait_nothing_will_end() -> None:
    """The judge that waited in a v12 run: no checkpoint was ever requested for it."""
    found = core.orphan_waits(_state(_yielded()))

    assert [(wait.waiter, wait.subject) for wait in found] == [
        (core.WaitKind.YIELD_PUBLICATION, "yield")
    ]


def test_a_yield_is_produced_by_its_checkpoint_while_that_runs() -> None:
    invocation = _yielded("checkpoint")
    running = _state(invocation, _checkpoint(invocation, core.RequestPhase.DISPATCHED))

    assert core.orphan_waits(running) == ()
    assert [wait.producer for wait in core.waits(running)] == [core.Producer.PENDING_REQUEST]


def test_a_yield_whose_checkpoint_ended_without_publishing_is_a_wait_nothing_will_end() -> None:
    invocation = _yielded("checkpoint")
    ended = _state(invocation, _checkpoint(invocation, core.RequestPhase.COMPLETED))

    assert [wait.waiter for wait in core.orphan_waits(ended)] == [core.WaitKind.YIELD_PUBLICATION]


_PHASES = st.sampled_from(list(core.RequestPhase))
_OPEN = {core.RequestPhase.PREPARED, core.RequestPhase.DISPATCHED, core.RequestPhase.RECONCILING}


@settings(max_examples=examples(pr=30, nightly=1000), deadline=None)
@given(phases=st.lists(_PHASES, max_size=3), unrelated=st.lists(_PHASES, max_size=2))
def test_a_yield_is_an_orphan_exactly_when_its_recorded_wait_names_nothing_open(
    phases: list[core.RequestPhase], unrelated: list[core.RequestPhase]
) -> None:
    """The verdict reads only the requests the yield named (#2428), not every checkpoint of
    the turn: an open checkpoint the yield does not name keeps nothing alive."""
    named = [f"named-{index}" for index in range(len(phases))]
    invocation = _yielded(*named)
    records = [
        _checkpoint(invocation, phase).model_copy(update={"request_id": core.RequestId(root=name)})
        for name, phase in zip(named, phases, strict=True)
    ]
    records += [
        _checkpoint(invocation, phase).model_copy(
            update={"request_id": core.RequestId(root=f"other-{index}")}
        )
        for index, phase in enumerate(unrelated)
    ]
    state = _state(invocation, *records)

    orphaned = [w for w in core.orphan_waits(state) if w.waiter == core.WaitKind.YIELD_PUBLICATION]

    assert bool(orphaned) == (not _OPEN.intersection(phases))
