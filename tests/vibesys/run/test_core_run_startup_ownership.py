"""The product core runner closes what it opened when startup does not complete."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest

import vs_proto.api as p
from vibesys.events import CoreEventType, IntentStateData, RunEndingData
from vibesys.run import EventJournal
from vibesys.run.core_run import drive_core_run
from vibesys.run.core_services import ClosableEvaluation, CoreServices
from vibesys.run.environment_failure import RunEnvironmentFailure
from vibesys.run.host import CoreHost
from vibesys.run.run_clock import RunClock
from vs_core.api import Flow, OperationRegistry, RunResultProposal, RunStatus, initial_state
from vs_core.api.search import search
from vs_project.api import Project
from vs_project.api.testing import FakeControlInbox
from vs_runtime.api import ArtifactStore
from vs_runtime.api.core import IgnoreCommits, RunTiming
from vs_runtime.api.core_shell_fixtures import CounterConfig
from vs_runtime.api.infrastructure import (
    PauseRequest,
    RunControlTransitionKind,
    RunStopped,
    create_run_control_channel,
)
from vs_runtime.api.step_runner import StepLoopStalledError
from vs_runtime.api.testing import FakeRunControlEventSink, FakeStepRunner
from vs_sim.api.testing import SimThreads, VirtualClock, run_virtual

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vibesys.run.integration import LocalRunIntegration
    from vs_core.api import CoreState
    from vs_project.api import ControlInbox
    from vs_runtime.api import Evaluation, RunFacts
    from vs_runtime.api.core import AgentEvaluationBridge, OperationCatalog
    from vs_runtime.api.infrastructure import (
        OwnedWorkspaces,
        RunControlChannel,
        RunControlEventSink,
        RunControlTransition,
        WorkspaceHolds,
    )


@search("startup-ownership", config=CounterConfig)
async def counter(ctx: Flow, config: CounterConfig) -> None:
    """A checkpointed idle search can reopen after an earlier epoch ended."""
    ctx.checkpoint(config)
    await ctx.first(ctx.stopped)


class BridgeStartError(RuntimeError):
    """The test bridge's listener service could not start."""

    def __init__(self) -> None:
        super().__init__("bridge startup interrupted")


class BridgeStartCancelled(asyncio.CancelledError):
    """The test bridge's listener startup was cancelled."""

    def __init__(self) -> None:
        super().__init__("bridge startup interrupted")


class BridgeCloseError(RuntimeError):
    """The test bridge could not release its listener."""

    def __init__(self) -> None:
        super().__init__("bridge close failed")


class _FailingBridge:
    """An evaluation bridge that owns its attached run and then fails to serve."""

    def __init__(
        self,
        start_error: type[BaseException] = BridgeStartError,
        *,
        close_fails: bool = False,
    ) -> None:
        self.attached = False
        self.closed = False
        self._start_error = start_error
        self._close_fails = close_fails

    def attach(self, _run: object, _clock: object) -> None:
        self.attached = True

    async def serve(self) -> None:
        raise self._start_error

    async def close(self) -> None:
        self.closed = True
        if self._close_fails:
            raise BridgeCloseError

    def unacknowledged(self) -> tuple[str, ...]:
        return ()


class _FollowingWorkspaces:
    """Minimal owned-workspace fake for the startup ownership boundary."""

    def __init__(self) -> None:
        self.holds: Callable[[], WorkspaceHolds] | None = None

    def follow(self, holds: Callable[[], WorkspaceHolds]) -> None:
        self.holds = holds

    async def release_unheld(self) -> None:
        assert self.holds is not None


@dataclass(frozen=True)
class _Integration:
    control: RunControlChannel
    events: EventJournal
    run_control: object = None

    @property
    def environment_failure(self) -> RunEnvironmentFailure:
        """An environment that never fails: these runs end on their own result."""
        return RunEnvironmentFailure(self.control)


def _declared_state() -> CoreState:
    state = initial_state()
    return state.model_copy(
        update={"run": state.run.model_copy(update={"declaration": counter.declaration})}
    )


@dataclass(frozen=True)
class _Ended:
    """A run whose committed core state has already ended, with an inbox and a control sink."""

    state: CoreState
    inbox: ControlInbox
    sink: RunControlEventSink


def _startup_host(
    tmp_path: Path,
    bridge: _FailingBridge | None,
    *,
    ended: _Ended | None = None,
) -> tuple[CoreHost, LocalRunIntegration, VirtualClock]:
    """Build the public product boundary around the configured bridge fault.

    ``ended`` starts the run from a TERMINAL core state, over its inbox and control sink.
    """
    project = Project.open(tmp_path)
    clock = VirtualClock(at=1.0)
    threads = SimThreads(clock)
    namespace = project.state.state_store_namespace("startup-ownership")
    services = CoreServices(
        run_id="startup-ownership",
        project=project,
        search=counter,
        config=CounterConfig(),
        state=_declared_state() if ended is None else ended.state,
        registry=OperationRegistry(),
        catalog=cast("OperationCatalog", object()),
        steering=None,
        commit_observer=IgnoreCommits(),
        runner=lambda step_clock, _epoch: FakeStepRunner(step_clock),
        evaluation=cast("ClosableEvaluation", object()),
        artifacts=ArtifactStore(namespace),
        clock=RunClock(clock),
        agent_evaluation=None if bridge is None else cast("AgentEvaluationBridge", bridge),
        control_inbox=None if ended is None else ended.inbox,
    )
    workspaces = _FollowingWorkspaces()
    host = CoreHost(
        cast("OwnedWorkspaces", workspaces),
        cast("Evaluation", object()),
        cast("RunFacts", object()),
        services,
        RunTiming(clock, threads),
    )
    journal = EventJournal(clock, threads)
    journal.enable_runlog(project, "startup-ownership")
    journal.attach(Project.log_directory_for(tmp_path, "startup-ownership"), "startup-ownership")
    integration = cast(
        "LocalRunIntegration",
        _Integration(
            create_run_control_channel(
                FakeRunControlEventSink() if ended is None else ended.sink, threads=threads
            ),
            journal,
        ),
    )
    return host, integration, clock


def test_a_bridge_startup_failure_closes_the_bridge_and_ends_the_run_resumable(
    tmp_path: Path,
) -> None:
    """Once the run's log is open, every bridge-start exit still closes the bridge."""
    bridge = _FailingBridge()
    host, integration, clock = _startup_host(tmp_path, bridge)

    async def exercise() -> None:
        with pytest.raises(BridgeStartError, match="bridge startup interrupted"):
            await drive_core_run(host, integration)

    run_virtual(clock, exercise())

    assert bridge.closed
    assert bridge.attached
    # The run recorded no result, so a resume continues it from its first round.
    events = cast("_Integration", integration).events
    assert [(e.type, e.data) for e in events.history()] == [
        (
            CoreEventType.RUN_ENDING,
            RunEndingData(reason=None, resumable=True, rounds_finished=0),
        )
    ]


@pytest.mark.parametrize("start_error", [BridgeStartError, BridgeStartCancelled])
def test_a_bridge_startup_interruption_survives_a_close_failure(
    tmp_path: Path, start_error: type[BaseException]
) -> None:
    """Bridge cleanup cannot replace the startup failure or cancellation it followed."""
    bridge = _FailingBridge(start_error, close_fails=True)
    host, integration, clock = _startup_host(tmp_path, bridge)

    async def exercise() -> BaseException:
        with pytest.raises(start_error, match="bridge startup interrupted") as failure:
            await drive_core_run(host, integration)
        return failure.value

    failure = run_virtual(clock, exercise())

    assert failure.__notes__ == [
        "Additional error while closing the agent evaluation bridge: "
        "BridgeCloseError: bridge close failed"
    ]
    assert bridge.closed


class SinkError(OSError):
    """The run's control journal could not be written."""

    def __init__(self) -> None:
        super().__init__("control journal unwritable")


class _FailingSink:
    """A control sink whose journal write fails."""

    def __call__(self, transition: RunControlTransition) -> object:
        del transition
        raise SinkError


def _ended_run(sink: RunControlEventSink) -> _Ended:
    """A run core already ended, with a pause posted to its inbox that it never applied."""
    state = _declared_state()
    result = RunResultProposal(outcome="cancelled", reason="stopped: by request")
    state = state.model_copy(
        update={
            "run": state.run.model_copy(update={"status": RunStatus.TERMINAL, "result": result})
        }
    )
    inbox = FakeControlInbox()
    inbox.post("p1", PauseRequest(control_id="p1").model_dump_json().encode(), ended=_live)
    return _Ended(state, inbox, sink)


def _live() -> None:
    return None


@pytest.mark.parametrize("bridge", [None, _FailingBridge()], ids=["returns", "raises"])
def test_an_ended_run_rejects_what_its_inbox_still_holds(
    tmp_path: Path, bridge: _FailingBridge | None
) -> None:
    """The run answers every request left once it has ended, also when its loop raised."""
    sink = FakeRunControlEventSink()
    ended = _ended_run(sink)
    host, integration, clock = _startup_host(tmp_path, bridge, ended=ended)

    async def exercise() -> None:
        with contextlib.suppress(BridgeStartError, RunStopped, StepLoopStalledError):
            await drive_core_run(host, integration)

    run_virtual(clock, exercise())

    assert ended.inbox.pending() == ()
    assert [(t.kind, t.request_id) for t in sink.transitions] == [
        (RunControlTransitionKind.REQUEST_REJECTED, "p1")
    ]
    assert_terminal_refusal(cast("_Integration", integration).events)


@pytest.mark.parametrize("bridge", [None, _FailingBridge()], ids=["returns", "raises"])
def test_a_failure_answering_the_last_controls_does_not_replace_the_loops_error(
    tmp_path: Path, bridge: _FailingBridge | None
) -> None:
    """The run's end is reported; the loop's own error is not replaced."""
    host, integration, clock = _startup_host(tmp_path, bridge, ended=_ended_run(_FailingSink()))

    async def exercise() -> BaseException:
        with pytest.raises((SinkError, BridgeStartError)) as failure:
            await drive_core_run(host, integration)
        return failure.value

    failure = run_virtual(clock, exercise())

    if bridge is None:
        assert isinstance(failure, SinkError)
    else:
        assert isinstance(failure, BridgeStartError)
        assert failure.__notes__ == [
            "Additional error while answering the run's last controls: "
            "SinkError: control journal unwritable"
        ]
    events = cast("_Integration", integration).events
    assert [e.type for e in events.history() if e.type is not CoreEventType.INTENT_STATE] == [
        CoreEventType.RUN_ENDING
    ]
    assert_terminal_refusal(events)


def assert_terminal_refusal(events: EventJournal) -> None:
    outcomes = [
        p.IntentState.from_json(e.data.state_json)
        for e in events.history()
        if isinstance(e.data, IntentStateData)
    ]
    assert [(o.control_id, o.step) for o in outcomes] == [("p1", p.IntentStep.REFUSED)]
    assert outcomes[0].reason
