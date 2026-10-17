"""Mixed public inputs share the same durable order and search callback cursor."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    ClockAdvanced,
    ContractError,
    ControlId,
    ControlInput,
    Flow,
    FlowState,
    InputId,
    PauseOwner,
    RunControlEvent,
    Scope,
    ScopeInputTarget,
    SessionInput,
    SessionInputReceived,
    initial_state,
)
from vs_core.flows import search
from vs_project.api import FakeStateStore, Project, StoredEnvelope
from vs_runtime.api.core import CoreRuntime, CoreRuntimeBindings, RuntimeRecord
from vs_runtime.api.core_shell_fixtures import CounterConfig, ShellTraceTransitions

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project.api import StateStore


class QueueInput(StrEnum):
    CLOCK = "clock"
    CONTROL = "control"
    OCCURRENCE = "occurrence"
    TICK = "tick"


@search("ticking", config=CounterConfig)
async def ticking(ctx: Flow, config: CounterConfig) -> None:
    """A search that, whenever it is woken at time t, asks to be woken again at t + 1."""
    del config
    at = 1.0
    while True:
        await ctx.first(ctx.timer(at))
        at += 1


def ticking_runtime(store: StateStore) -> CoreRuntime[FlowState]:
    core = initial_state()
    declared = core.model_copy(
        update={"run": core.run.model_copy(update={"declaration": ticking.declaration})}
    )
    return CoreRuntime(
        store,
        ticking.driver(CounterConfig()),
        declared,
        config=CounterConfig(),
        bindings=CoreRuntimeBindings(transitions=ShellTraceTransitions()),
    )


def occurrence(index: int) -> SessionInputReceived:
    return SessionInputReceived(
        input=SessionInput(
            input_id=InputId(root=f"input-{index}"),
            target=ScopeInputTarget(scope=Scope(owner=initial_state().run.run_id, generation=0)),
            artifact=ArtifactRef(
                artifact_id=ArtifactId(root="shared-content"), digest="same-content"
            ),
            received_at=index,
            sequence=index,
        )
    )


@given(kinds=st.lists(st.sampled_from(list(QueueInput)), min_size=1, max_size=20))
def test_mixed_queue_commits_input_occurrences_controls_and_wakes_in_order(
    kinds: list[QueueInput],
) -> None:
    store = FakeStateStore()
    shell = ticking_runtime(store)
    shell.start("first", now_at=0, lease_duration=100)
    initial = shell.record
    for index, kind in enumerate(kinds, start=1):
        match kind:
            case QueueInput.CLOCK:
                shell.submit(ClockAdvanced(now_at=index), now_at=index)
            case QueueInput.CONTROL:
                shell.submit(
                    RunControlEvent(
                        control=ControlInput(
                            control_id=ControlId(root=f"control-{index}"), action="steer"
                        ),
                        now_at=index,
                    ),
                    now_at=index,
                )
            case QueueInput.OCCURRENCE:
                shell.submit(occurrence(index), now_at=index)
            case QueueInput.TICK:
                shell.flush(now_at=index)
    assert shell.record == initial
    callbacks = controls = inputs = ticks = events = 0
    for index, kind in enumerate(kinds, start=1):
        assert shell.advance()
        callbacks += kind in (QueueInput.CLOCK, QueueInput.CONTROL, QueueInput.TICK)
        events += kind in (QueueInput.CLOCK, QueueInput.CONTROL)
        controls += kind == QueueInput.CONTROL
        inputs += kind == QueueInput.OCCURRENCE
        ticks += kind == QueueInput.TICK
        assert shell.storage_revision == index
        assert shell.record.envelope.core.revision == index + 1 - ticks
        assert len(shell.record.envelope.strategy.journal) == 1 + callbacks
        assert len(shell.record.envelope.core.run.controls) == controls
        assert len(shell.record.envelope.core.sessions.inputs) == inputs
        assert shell.record.envelope.event_cursor.sequence == events
        stored = store.load()
        assert isinstance(stored, StoredEnvelope)
        assert RuntimeRecord[FlowState].model_validate_json(stored.payload) == shell.record
    assert not shell.advance()
    before_inputs = shell.record.envelope.core.sessions.inputs
    restarted = ticking_runtime(store)
    restarted.start("second", now_at=100, lease_duration=100)
    assert restarted.record.envelope.core.sessions.inputs == before_inputs
    for row in before_inputs:
        restarted.submit(SessionInputReceived(input=row.input), now_at=100)
        assert restarted.advance()
    assert restarted.record.envelope.core.sessions.inputs == before_inputs


def test_an_operator_control_core_refuses_is_set_aside_and_the_run_goes_on() -> None:
    """Core refuses a control the bridge judged against an older state; the loop goes on.

    Here a wait for capacity reaches a run that is not paused. The control is set aside
    with core's reason for the control bridge, which answers the operator; core records
    nothing for it. Before, the refusal propagated out of the loop and ended the run.
    """
    shell = ticking_runtime(FakeStateStore())
    shell.start("first", now_at=0, lease_duration=100)
    before = shell.record
    refused = ControlInput(
        control_id=ControlId(root="op-w1"), action="resume", when="capacity_returns"
    )
    steer = ControlInput(control_id=ControlId(root="op-s1"), action="steer")
    shell.submit(RunControlEvent(control=refused, now_at=1), now_at=1)
    shell.submit(RunControlEvent(control=steer, now_at=2), now_at=2)

    assert shell.advance()
    assert shell.record == before
    assert shell.advance()
    assert [c.control_id.root for c in shell.record.envelope.core.run.controls] == ["op-s1"]
    assert [(error.control, error.detail) for error in shell.take_refused()] == [
        (refused, "only a paused run waits for capacity")
    ]
    assert shell.take_refused() == ()


def test_a_control_core_holds_with_another_payload_still_fails_the_loop() -> None:
    """Regression (R2 of the #3091 re-check): only core's ``control_refusal`` is set aside.

    A control under an identity core holds for a different control is a defect in whatever
    built it (the control bridge), not a refusal an operator can be told about. Before, the
    loop dropped every rejected control, so this became a warning and the run went on.
    """
    shell = ticking_runtime(FakeStateStore())
    shell.start("first", now_at=0, lease_duration=100)
    steer = ControlInput(control_id=ControlId(root="op-x"), action="steer")
    shell.submit(RunControlEvent(control=steer, now_at=1), now_at=1)
    assert shell.advance()
    clash = ControlInput(
        control_id=ControlId(root="op-x"), action="pause", owner=PauseOwner.OPERATOR
    )
    shell.submit(RunControlEvent(control=clash, now_at=2), now_at=2)

    with pytest.raises(ContractError, match="control identity payload conflict"):
        shell.advance()
    assert shell.take_refused() == ()


def test_disk_reopen_loads_identical_complete_runtime_record(tmp_path: Path) -> None:
    project = Project.open(tmp_path)
    store = project.state_store("run")
    shell = ticking_runtime(store)
    shell.start("first", now_at=0, lease_duration=10)
    shell.submit(ClockAdvanced(now_at=1), now_at=1)
    shell.submit(occurrence(2), now_at=2)
    shell.flush(now_at=3)
    while shell.advance():
        pass
    reopened = Project.open(tmp_path).state_store("run")
    stored = reopened.load()
    assert isinstance(stored, StoredEnvelope)
    assert RuntimeRecord[FlowState].model_validate_json(stored.payload) == shell.record
    next_shell = ticking_runtime(reopened)
    next_shell.start("second", now_at=10, lease_duration=10)
    assert next_shell.record.envelope.strategy == shell.record.envelope.strategy
    assert (
        next_shell.record.envelope.core.sessions.inputs
        == shell.record.envelope.core.sessions.inputs
    )
