"""An operator's action applies alike in every built-in search, through the run's control path.

Each search (single, multi, evolve, dynamic, issue queue) runs its full pipeline over core with
the Fake shell. While the first turn of an attempt runs, the operator submits one action the
way every ingress does (``ChannelRunControl.submit``), and the run's control bridge takes it at
that poll. Whatever the search:

- a cancelled attempt, or every attempt at a cancelled item, closes cancelled and is never
  judged, and the run still ends;
- a new parallel limit is the one in force when the run ends;
- the action's notice, rendered from its template, is stored and reaches a turn (or is
  dropped by core because no turn was left to take it), never left waiting;
- the same cancel sent again under another id in the same poll changes nothing: core's rule
  refuses it, asked of the state the first cancel leads to, and the operator hears why
  instead of core being handed a control it would refuse.

The profile-guided and plain variants run the single and multi searches' code with other
options, so they are covered by those.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.dynamic_world import DynamicWorld
from tests.vibesys.evolve_world import EvolveWorld
from tests.vibesys.orchestration.issue_queue._replies import evaluated, implemented, judged
from tests.vibesys.orchestration.issue_queue.world import IssueQueueWorld
from tests.vibesys.serial_world import SerialWorld

import vs_core.api as core
from vs_core.testing.drive import drive
from vs_runtime.api.core import RunControlBridge
from vs_runtime.api.infrastructure import (
    CancelAttemptRequest,
    CancelItemRequest,
    ChannelRunControl,
    ControlStatus,
    RunControlTransitionKind,
    SetMaxParallelRequest,
    create_run_control_channel,
)
from vs_runtime.api.testing import FakeRunControlEventSink
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
    from vs_runtime.api.infrastructure import ActionRequest

_WORLDS = ("single", "multi", "evolve", "dynamic", "issue_queue")
_ACTIONS = ("cancel_attempt", "cancel_item", "set_max_parallel")


def _world(name: str) -> SerialWorld | EvolveWorld | DynamicWorld | IssueQueueWorld:
    match name:
        case "single" | "multi":
            return SerialWorld(name)
        case "evolve":
            return EvolveWorld(
                max_generations=1, children_per_generation=2, throughputs=(100.0, 150.0)
            )
        case "dynamic":
            return DynamicWorld()
        case _:
            return IssueQueueWorld(
                implementer=[implemented(f"turn {n}") for n in range(2)],
                judge=[judged(passed=False), judged()],
                perf=[evaluated()],
            )


@dataclass
class _Texts:
    held: dict[str, str] = field(default_factory=dict)

    def put(self, ref: core.ArtifactRef, text: str) -> None:
        self.held[ref.artifact_id.root] = text


@dataclass
class _Operator:
    """Acts once, during the first turn of an attempt, through the run's control path."""

    action: str
    sink: FakeRunControlEventSink = field(default_factory=FakeRunControlEventSink)
    texts: _Texts = field(default_factory=_Texts)
    sent: list[ActionRequest] = field(default_factory=list)
    subject: str | None = None

    def __post_init__(self) -> None:
        threads = SimThreads()
        channel = create_run_control_channel(self.sink, threads=threads)
        self.control = ChannelRunControl(channel, threads)
        self.bridge = RunControlBridge(channel, self.texts, control=self.control)

    def during_turn(
        self, request: core.Request, state: core.CoreState
    ) -> tuple[core.CoreEvent, ...]:
        if (
            self.sent
            or not isinstance(request, core.DispatchTurn)
            or request.scope.owner == state.run.run_id
        ):
            return ()
        owner = request.scope.owner.root
        row = next(r for r in state.attempts.attempts if r.attempt_id.root == owner)
        match self.action:
            case "cancel_attempt":
                self.subject = owner
                # The second, under another id, is the same cancel: it can change nothing.
                self.sent = [
                    CancelAttemptRequest(control_id="c1", attempt=owner),
                    CancelAttemptRequest(control_id="c2", attempt=owner),
                ]
            case "cancel_item":
                self.subject = row.item_id.root
                self.sent = [CancelItemRequest(control_id="c1", item=row.item_id.root)]
            case _:
                self.sent = [SetMaxParallelRequest(control_id="c1", max_parallel=1)]
        for sent in self.sent:
            assert self.control.submit(sent).status is ControlStatus.PENDING
        return self.bridge.poll(state, state.run.now_at)

    def refused(self) -> list[str]:
        return [
            t.text
            for t in self.sink.transitions
            if t.kind is RunControlTransitionKind.CONTROL_REFUSED
        ]


def _cancelled(trace_core: core.CoreState, operator: _Operator) -> set[str]:
    """The attempts the action meant to cancel: the attempt, or every attempt at the item."""
    if operator.action == "cancel_attempt":
        return {operator.subject or ""}
    return {
        row.attempt_id.root
        for row in trace_core.attempts.attempts
        if row.item_id.root == operator.subject
    }


@pytest.mark.parametrize("action", _ACTIONS)
@pytest.mark.parametrize("world_name", _WORLDS)
def test_an_operator_action_applies_alike_in_every_search(world_name: str, action: str) -> None:
    world = _world(world_name)
    operator = _Operator(action)
    harness = dataclasses.replace(
        world.setup.harness, shell_events=True, during_turn=operator.during_turn
    )
    launch = world.setup.new_launch()

    trace = drive(
        launch.search.driver(launch.config, harness.registry), launch.config, world, harness
    )

    assert trace.finished, "the run did not end"
    assert trace.core.run.status is core.RunStatus.TERMINAL
    assert operator.sent, "no attempt ran a turn to act on"
    applied = [c for c in trace.core.run.controls if c.action == action]
    assert [c.control_id.root for c in applied] == ["op-c1"]
    (control,) = applied
    notice = next(
        row
        for row in trace.core.sessions.inputs
        if row.input.input_id == core.notice_input_id(control)
    )
    assert notice.receipt is not None, "the notice is still waiting in an ended run"
    assert control.artifact is not None
    assert operator.texts.held[control.artifact.artifact_id.root].startswith("The operator ")
    if action == "set_max_parallel":
        assert core.parallel_limit(trace.core.run) == 1
        assert operator.refused() == []
        return
    outcomes = {
        row.attempt.attempt_id.root: row.outcome for row in trace.core.settlement.settlements
    }
    for attempt in _cancelled(trace.core, operator):
        assert outcomes.get(attempt) == "cancelled", (attempt, outcomes)
    if action == "cancel_attempt":
        assert operator.refused() == ["cancel_attempt c2: the attempt has ended"]
    else:
        assert operator.refused() == []
