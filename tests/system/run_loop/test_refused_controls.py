"""An operator control the run refuses reaches the operator, through the production run loop.

The loop (``vs_runtime._core_run``) drives the real shell, the real control bridge and the
real channel; the operator acts through the channel's controls and the run's inbox, as the
server's socket and ``vibesys control`` do. A control the run refuses after the channel
took it is answered with core's reason, and the channel's pause is core's again: whether
the bridge refuses it when it judges the channel's change (the run is finishing), or core
refuses it when it commits (an input committed first ended the run). Nothing here polls
after the run ends, which the loop never does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.support.skeleton_strategy import SkeletonStrategy
from tests.support.skeleton_world import open_skeleton_world

from vs_core.api import (
    ControlId,
    ControlInput,
    RunControlEvent,
    RunResultProposal,
    RunStatus,
    StopMode,
)
from vs_project.api.testing import FakeControlInbox
from vs_runtime.api.core import (
    CoreRunHost,
    OperatorInbox,
    RunControlBridge,
    RunLoopConfig,
    drive_core,
    start_core,
)
from vs_runtime.api.infrastructure import (
    ChannelRunControl,
    PauseNowRequest,
    PauseRequest,
    RunControlTransitionKind,
    RuntimeRunControlChannel,
)
from vs_runtime.api.testing import FakeRunControlEventSink, ProbedRunClock
from vs_sim.api.testing import SimThreads, clock_from

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_core.api import ArtifactRef, CoreState
    from vs_sim.api.testing import VirtualClock

_CONFIG = RunLoopConfig(host_id="loop", lease_duration=300.0, control_poll_interval=1.0)


def _live() -> str | None:
    """A poster's view of a run that has not ended."""
    return None


class _Texts:
    def put(self, ref: ArtifactRef, text: str) -> None:
        del ref, text


class _Operator(ProbedRunClock):
    """The loop's clock, with an operator who acts once, at the first wait ``when`` holds.

    The operator acts as a wait begins, so the next iteration's poll sees what it did.
    """

    def __init__(self, inner: VirtualClock) -> None:
        """Wrap the loop's clock; ``core`` and the actions are set before the run starts."""
        super().__init__(inner)
        self.core: Callable[[], CoreState] | None = None
        self.actions: list[tuple[Callable[[CoreState], bool], Callable[[], object]]] = []

    async def sleep(self, seconds: float) -> None:
        """Run the first action whose condition the committed state meets, then advance."""
        assert self.core is not None
        core = self.core()
        due = next((pair for pair in self.actions if pair[0](core)), None)
        if due is not None:
            self.actions.remove(due)
            due[1]()
        await super().sleep(seconds)


class _Wiring:
    """One run's channel, its operators' controls, its inbox and what the channel reported."""

    def __init__(self) -> None:
        threads = SimThreads()
        self.sink = FakeRunControlEventSink()
        self.channel = RuntimeRunControlChannel(self.sink, threads=threads)
        self.control = ChannelRunControl(self.channel, threads)
        self.inbox = FakeControlInbox()
        self.bridge = RunControlBridge(
            self.channel, _Texts(), inbox=OperatorInbox(self.inbox, self.control)
        )

    def refusals(self) -> list[str]:
        return [
            t.text
            for t in self.sink.transitions
            if t.kind is RunControlTransitionKind.CONTROL_REFUSED
        ]


def _always(core: CoreState) -> bool:
    del core
    return True


async def _run(
    tmp_path: Path,
    strategy: SkeletonStrategy,
    wiring: _Wiring,
    actions: Callable[[_Operator, CoreRunHost], None],
    *,
    timed: float | None = None,
) -> CoreState:
    clock = _Operator(clock_from(1.0))
    with open_skeleton_world(
        tmp_path, strategy, timed=None if timed is None else (clock, timed)
    ) as world:
        process = world.runtime()
        host = CoreRunHost(process.shell, process.delivery, clock, wiring.bridge)
        clock.core = lambda: process.shell.record.envelope.core
        actions(clock, host)
        start_core(host, _CONFIG)
        outcome = await drive_core(host, _CONFIG)
        assert outcome.status == RunStatus.TERMINAL
        return process.shell.record.envelope.core


def _ends_first(host: CoreRunHost) -> None:
    """An input that ends the run, queued while the loop waited, ahead of the next poll.

    It stands for any input another submitter queues between two polls (an agent's tool
    call is admitted that way); core commits it before the controls that poll emits.
    """
    stop = ControlInput(control_id=ControlId(root="ended"), action="stop", mode=StopMode.CANCEL)
    result = RunResultProposal(outcome="blocked", reason="ended by the test")
    host.shell.submit(RunControlEvent(control=stop, result=result, now_at=1.0), now_at=1.0)


async def test_a_socket_pause_the_bridge_refuses_after_a_stop_is_answered(tmp_path: Path) -> None:
    """Regression (R1 of the #3091 re-check), the bridge refusing a socket control.

    The operator stops the run while its baseline job runs, then pauses it through the
    socket before the search has ended the run. Core takes no pause once it recorded a
    stop, so the bridge refuses the channel's hold: the operator hears core's reason and
    the hold is lifted. Before, the bridge ignored the channel's pause after a stop: the
    ack said pending, nothing answered it, and the channel stayed paused while core was not.
    """
    wiring = _Wiring()

    def actions(clock: _Operator, host: CoreRunHost) -> None:
        del host

        def measuring(core: CoreState) -> bool:
            return any(job.status == "pending" for job in core.evaluation.jobs)

        def stopped(core: CoreState) -> bool:
            recorded = {control.control_id for control in core.run.controls}
            return ControlId(root="op-s1") in recorded and core.run.status != RunStatus.TERMINAL

        clock.actions += [
            (measuring, lambda: wiring.control.submit(PauseNowRequest(control_id="s1"))),
            (stopped, lambda: wiring.control.submit(PauseRequest(control_id="p1"))),
        ]

    core = await _run(tmp_path, SkeletonStrategy.baseline_only(), wiring, actions, timed=150.0)

    assert wiring.channel.pause_state() is None
    assert wiring.refusals() == ["pause p1: a stopped run takes no pause, only a resume"]
    assert ControlId(root="op-p1") not in {c.control_id for c in core.run.controls}


async def test_a_socket_pause_core_refuses_at_the_commit_that_ends_the_run_is_answered(
    tmp_path: Path,
) -> None:
    """Regression (R1 of the #3091 re-check), core refusing a socket control at commit.

    The bridge judges the pause against a running run and emits it, but an input queued
    first ends the run in the same commit, so core refuses the pause. The loop then exits
    without another poll. The operator hears core's reason, and the channel is not left
    holding a pause core never recorded. Before, the loop only logged the refusal.
    """
    wiring = _Wiring()

    def actions(clock: _Operator, host: CoreRunHost) -> None:
        def operator_and_end() -> None:
            wiring.control.submit(PauseRequest(control_id="p1"))
            _ends_first(host)

        clock.actions.append((_always, operator_and_end))

    core = await _run(tmp_path, SkeletonStrategy.waiting_until(1000.0), wiring, actions)

    assert core.run.result is not None
    assert wiring.channel.pause_state() is None
    assert wiring.refusals() == ["pause p1: terminal run rejects controls"]


async def test_an_inbox_pause_refused_by_the_commit_that_ends_the_run_is_rejected(
    tmp_path: Path,
) -> None:
    """Regression (R1 of the #3091 re-check), an inbox request refused as the run ends.

    As above, through the run's inbox: the request is rejected with core's reason before
    the loop exits, and the channel's pause is core's. Before, the loop broke on the
    terminal status before the bridge heard of the refusal, and nothing else rejects an
    inbox request, so it stayed pending for good.
    """
    wiring = _Wiring()

    def actions(clock: _Operator, host: CoreRunHost) -> None:
        def operator_and_end() -> None:
            wiring.inbox.post(
                "p1", PauseRequest(control_id="p1").model_dump_json().encode(), ended=_live
            )
            _ends_first(host)

        clock.actions.append((_always, operator_and_end))

    await _run(tmp_path, SkeletonStrategy.waiting_until(1000.0), wiring, actions)

    assert wiring.inbox.pending() == ()
    assert [(entry.request_id, entry.reason) for entry in wiring.inbox.rejected()] == [
        ("p1", "refused by the run: terminal run rejects controls")
    ]
    assert wiring.refusals() == []  # the inbox's rejection is the operator's answer
    assert wiring.channel.pause_state() is None
