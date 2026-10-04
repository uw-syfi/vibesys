"""Mixed public inputs share the same durable order and strategy callback cursor."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st
from tests.support.runtime_core_shell import CounterState, runtime

from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    ClockAdvanced,
    ControlId,
    ControlInput,
    InputId,
    RunControlEvent,
    Scope,
    ScopeInputTarget,
    SessionInput,
    SessionInputReceived,
    initial_state,
)
from vs_project.api import FakeStateStore, Project, StoredEnvelope
from vs_runtime.api.core import RuntimeRecord

if TYPE_CHECKING:
    from pathlib import Path


class QueueInput(StrEnum):
    CLOCK = "clock"
    CONTROL = "control"
    OCCURRENCE = "occurrence"
    PROPOSAL = "proposal"


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
def test_mixed_queue_commits_input_occurrences_controls_and_proposals_in_order(
    kinds: list[QueueInput],
) -> None:
    store = FakeStateStore()
    shell = runtime(store)
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
            case QueueInput.PROPOSAL:
                shell.decide(now_at=index)
    assert shell.record == initial
    callbacks = controls = inputs = proposals = 0
    for index, kind in enumerate(kinds, start=1):
        assert shell.advance()
        callbacks += kind in (QueueInput.CLOCK, QueueInput.CONTROL)
        controls += kind == QueueInput.CONTROL
        inputs += kind == QueueInput.OCCURRENCE
        proposals += kind == QueueInput.PROPOSAL
        assert shell.record.envelope.core.revision == index + 1
        assert shell.record.envelope.strategy.callbacks == callbacks
        assert shell.record.envelope.strategy.proposals == proposals
        assert len(shell.record.envelope.core.run.controls) == controls
        assert len(shell.record.envelope.core.sessions.inputs) == inputs
        assert shell.record.envelope.event_cursor.sequence == callbacks
        stored = store.load()
        assert isinstance(stored, StoredEnvelope)
        assert RuntimeRecord[CounterState].model_validate_json(stored.payload) == shell.record
    assert not shell.advance()
    before_inputs = shell.record.envelope.core.sessions.inputs
    restarted = runtime(store)
    restarted.start("second", now_at=100, lease_duration=100)
    assert restarted.record.envelope.core.sessions.inputs == before_inputs
    for row in before_inputs:
        restarted.submit(SessionInputReceived(input=row.input), now_at=100)
        assert restarted.advance()
    assert restarted.record.envelope.core.sessions.inputs == before_inputs


def test_disk_reopen_loads_identical_complete_runtime_record(tmp_path: Path) -> None:
    project = Project.open(tmp_path)
    store = project.state_store("run")
    shell = runtime(store)
    shell.start("first", now_at=0, lease_duration=10)
    shell.submit(ClockAdvanced(now_at=1), now_at=1)
    shell.submit(occurrence(2), now_at=2)
    shell.decide(now_at=3)
    while shell.advance():
        pass
    reopened = Project.open(tmp_path).state_store("run")
    stored = reopened.load()
    assert isinstance(stored, StoredEnvelope)
    assert RuntimeRecord[CounterState].model_validate_json(stored.payload) == shell.record
    next_shell = runtime(reopened)
    next_shell.start("second", now_at=10, lease_duration=10)
    assert next_shell.record.envelope.strategy == shell.record.envelope.strategy
    assert (
        next_shell.record.envelope.core.sessions.inputs
        == shell.record.envelope.core.sessions.inputs
    )
