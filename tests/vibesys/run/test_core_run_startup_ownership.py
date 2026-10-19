"""The product core runner rolls back resources when startup does not complete."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import pytest

from vibesys.events import CoreEventType, RunEndingData
from vibesys.run.core_run import CoreRunRefusedError, drive_core_run
from vibesys.run.core_services import ClosableEvaluation, CoreServices
from vibesys.run.environment_failure import RunEnvironmentFailure
from vibesys.run.host import CoreHost
from vs_core.api import RunResultProposal, RunStatus, initial_state
from vs_project.api import FakeStateStore, Project
from vs_project.api.testing import FakeControlInbox
from vs_runtime.api import ArtifactStore
from vs_runtime.api.core import CoreRuntimeBindings, RunTiming
from vs_runtime.api.core_shell_fixtures import CounterConfig, ShellTraceTransitions, counter
from vs_runtime.api.infrastructure import (
    PauseRequest,
    RunControlTransitionKind,
    RunStopped,
    create_run_control_channel,
)
from vs_runtime.api.testing import FakeRunControlEventSink
from vs_sim.api.testing import SimThreads, VirtualClock, run_virtual

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vibesys.events import CoreEventData
    from vibesys.run.integration import LocalRunIntegration
    from vs_core.api import CoreState
    from vs_project.api import ControlInbox
    from vs_runtime.api import Evaluation, RunFacts
    from vs_runtime.api.core import AgentEvaluationBridge, CoreRuntime, OperationCatalog
    from vs_runtime.api.infrastructure import (
        OwnedWorkspaces,
        RunControlChannel,
        RunControlEventSink,
        RunControlTransition,
        WorkspaceHolds,
    )


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
    """An evaluation bridge that owns its attached shell and then fails to serve."""

    def __init__(
        self,
        start_error: type[BaseException] = BridgeStartError,
        *,
        close_fails: bool = False,
    ) -> None:
        self.shell: CoreRuntime | None = None
        self.closed = False
        self._start_error = start_error
        self._close_fails = close_fails

    def attach(self, shell: CoreRuntime, _clock: object) -> None:
        self.shell = shell

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


@dataclass
class _Events:
    """Collects the events a core run emits."""

    emitted: list[tuple[CoreEventType, CoreEventData | None]] = field(default_factory=list)

    def emit(
        self, event_type: CoreEventType, text: str = "", *, data: CoreEventData | None = None
    ) -> None:
        del text
        self.emitted.append((event_type, data))


@dataclass(frozen=True)
class _Integration:
    control: RunControlChannel
    run_control: object = None
    events: _Events = field(default_factory=_Events)

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
    stop_requested: bool = False,
    ended: _Ended | None = None,
) -> tuple[CoreHost, LocalRunIntegration, VirtualClock]:
    """Build the public product boundary around the configured bridge fault.

    ``ended`` starts the run from a TERMINAL core state, over its inbox and control sink.
    """
    project = Project.open(tmp_path)
    clock = VirtualClock(at=1.0)
    threads = SimThreads(clock)
    store = FakeStateStore()
    # The trace fixture takes no control input; a run that must commit a stop uses core's own.
    bindings = (
        CoreRuntimeBindings()
        if stop_requested
        else CoreRuntimeBindings(transitions=ShellTraceTransitions())
    )
    namespace = project.state.state_store_namespace("startup-ownership")
    services = CoreServices(
        run_id="startup-ownership",
        search=counter,
        config=CounterConfig(),
        state=_declared_state() if ended is None else ended.state,
        bindings=bindings,
        catalog=cast("OperationCatalog", object()),
        store=store,
        publications=namespace,
        receipts=namespace,
        evaluation=cast("ClosableEvaluation", object()),
        artifacts=ArtifactStore(namespace),
        clock=clock,
        agent_evaluation=None if bridge is None else cast("AgentEvaluationBridge", bridge),
        stop_requested=stop_requested,
        control_inbox=None if ended is None else ended.inbox,
    )
    workspaces = _FollowingWorkspaces()
    host = CoreHost(
        cast("OwnedWorkspaces", workspaces),
        cast("Evaluation", object()),
        cast("RunFacts", object()),
        services,
        RunTiming(clock, 60.0, threads),
    )
    integration = cast(
        "LocalRunIntegration",
        _Integration(
            create_run_control_channel(
                FakeRunControlEventSink() if ended is None else ended.sink, threads=threads
            )
        ),
    )
    return host, integration, clock


def test_a_bridge_startup_failure_closes_the_bridge_and_releases_the_run_lease(
    tmp_path: Path,
) -> None:
    """Once core starts, every bridge-start exit still unwinds both product-owned resources."""
    bridge = _FailingBridge()
    host, integration, clock = _startup_host(tmp_path, bridge)

    async def exercise() -> None:
        with pytest.raises(BridgeStartError, match="bridge startup interrupted"):
            await drive_core_run(host, integration)

    run_virtual(clock, exercise())

    assert bridge.closed
    assert bridge.shell is not None
    assert not bridge.shell.holds_lease(now_at=clock.now())
    # The run recorded no result, so a resume continues it from its first round.
    events = cast("_Integration", integration).events
    assert events.emitted == [
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
    assert bridge.shell is not None
    assert not bridge.shell.holds_lease(now_at=clock.now())


def test_a_run_force_stopped_before_starts_no_new_work_and_goes_straight_to_its_end(
    tmp_path: Path,
) -> None:
    """A resume of a force-stopped run commits the recorded stop before asking its search.

    Core then ends the run as a stop does, by adopting its last choice: the only request
    the run makes is that adoption (refused here, where no workspace executor is bound),
    never an agent turn or an evaluation.
    """
    host, integration, clock = _startup_host(tmp_path, None, stop_requested=True)

    async def exercise() -> CoreRunRefusedError:
        with pytest.raises(CoreRunRefusedError) as refused:
            await drive_core_run(host, integration)
        return refused.value

    refusal = run_virtual(clock, exercise()).refusal

    assert refusal.request_id.root.startswith("adopt:")
    events = cast("_Integration", integration).events
    assert [event_type for event_type, _data in events.emitted] == [CoreEventType.RUN_ENDING]
    ((_type, ending),) = events.emitted
    assert isinstance(ending, RunEndingData)
    assert ending.rounds_finished == 0


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
        with contextlib.suppress(BridgeStartError, CoreRunRefusedError, RunStopped):
            await drive_core_run(host, integration)

    run_virtual(clock, exercise())

    assert ended.inbox.pending() == ()
    assert [(t.kind, t.request_id) for t in sink.transitions] == [
        (RunControlTransitionKind.REQUEST_REJECTED, "p1")
    ]


@pytest.mark.parametrize("bridge", [None, _FailingBridge()], ids=["returns", "raises"])
def test_a_failure_answering_the_last_controls_still_releases_the_lease(
    tmp_path: Path, bridge: _FailingBridge | None
) -> None:
    """The run's end is reported and its lease freed; the loop's own error is not replaced."""
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
    assert [event_type for event_type, _data in events.emitted] == [CoreEventType.RUN_ENDING]
    # Another host takes the run's lease at once: this one freed it rather than let it lapse.
    assert host.services.store.acquire("next-host", now=clock.now(), duration=1.0) is not None
