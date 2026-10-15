"""An operator steer reaches the next agent turn, once, through core's own input records.

The run is the production loop over the real shell with the Fake Slurm cluster and a Fake
provider. The turn message is rendered by the production ``TurnMessages`` from the
artifacts core reserved for the turn, so what the provider is sent is what a real run
would send.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tests.support.session_world import ProviderFaults, SessionHost
from tests.support.skeleton_world import (
    IMPLEMENTATION,
    IMPLEMENTER,
    LEASE,
    CandidateResolver,
    CandidateWriter,
    Implementation,
    open_skeleton_world,
)
from tests.support.workspace_world import RUN_ID

from vs_agent.api import AgentClient
from vs_agent.api.testing import FakeAgentInvocationStore, FakeProvider
from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    ControlId,
    ControlInput,
    InputDelivered,
    InputRecord,
    RunStatus,
    SessionInputReceived,
)
from vs_core.testing.builders import initial_state
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import ArtifactStore
from vs_runtime.api.core import (
    CoreRunHost,
    RunControlBridge,
    RunLoopConfig,
    TurnMessages,
    drive_core,
    start_core,
)
from vs_runtime.api.infrastructure import RuntimeRunControlChannel
from vs_runtime.api.testing import FakeRunControlEventSink
from vs_sim.api.testing import ManualClock, SimThreads, clock_from, loop_threads

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests.support.skeleton_world import World

    from vs_agent.api import AgentTurnRequest
    from vs_core.api import CoreState, SessionInput, TurnSpec
    from vs_prompts.api import RenderedPrompt

STEER = "focus on the KV cache"


@dataclass
class MessageResolver(CandidateResolver):
    """Resolves each turn's message with the production renderer over the run's artifacts."""

    messages: TurnMessages | None = None

    def message(self, turn: TurnSpec, inputs: tuple[SessionInput, ...]) -> RenderedPrompt | None:
        """The production turn message for the prompts and inputs reserved for this turn."""
        assert self.messages is not None
        return self.messages.render(turn, inputs)


@dataclass
class StoredSteers:
    """Where steer texts live: the run's artifact store, by content digest."""

    artifacts: ArtifactStore

    def put(self, ref: ArtifactRef, text: str) -> None:
        """Store the text; it must land under the digest the control carries."""
        assert self.artifacts.write(text.encode()).sha256 == ref.digest


@dataclass
class Sent:
    """Every message the provider was sent, in turn order."""

    messages: list[str] = field(default_factory=list)

    def record(self, request: AgentTurnRequest) -> None:
        """Remember one turn's message."""
        self.messages.append(request.message)


def _agents(sent: Sent) -> Callable[[Path], SessionHost]:
    def open_agents(root: Path) -> SessionHost:
        writer = CandidateWriter(root)

        def on_turn(request: AgentTurnRequest) -> None:
            sent.record(request)
            writer(request)

        threads = loop_threads()
        client = AgentClient(
            FakeProvider(answer=writer.answer, on_turn=on_turn, threads=threads),
            clock=ManualClock(),
            threads=threads,
        )
        resolver = MessageResolver(
            root,
            TemplateRenderer(root),
            roles=frozenset({IMPLEMENTER}),
            schemas={IMPLEMENTATION: Implementation},
            writer=writer,
        )
        return SessionHost(resolver, client, FakeAgentInvocationStore(), [], ProviderFaults())

    return open_agents


async def _run(world: World, channel: RuntimeRunControlChannel) -> CoreState:
    """Run to the terminal status; return core's final state."""
    artifacts = ArtifactStore(world.env.project.state.local_namespace(RUN_ID, "artifacts"))
    resolver = world.agents.resolver
    assert isinstance(resolver, MessageResolver)
    resolver.messages = TurnMessages(artifacts)
    process = world.runtime()
    bridge = RunControlBridge(channel, StoredSteers(artifacts))
    host = CoreRunHost(process.shell, process.delivery, clock_from(1.0), bridge)
    config = RunLoopConfig(
        host_id="steer", lease_duration=LEASE, recovery_poll_interval=50.0, max_dispatches=60
    )
    start_core(host, config)
    await drive_core(host, config)
    return process.shell.record.envelope.core


async def test_a_steer_queued_before_the_turn_reaches_it_once_and_is_recorded(
    tmp_path: Path,
) -> None:
    sent = Sent()
    sink = FakeRunControlEventSink()
    channel = RuntimeRunControlChannel(sink, threads=SimThreads())
    channel.queue_steer(STEER)
    with open_skeleton_world(tmp_path, agents=_agents(sent)) as world:
        core = await _run(world, channel)

    assert core.run.status == RunStatus.TERMINAL
    assert len(sent.messages) == 1
    assert sent.messages[0].count(STEER) == 1
    assert "operator" in sent.messages[0]
    # Core's record is the account: one input, delivered to the one turn that was sent it.
    [record] = core.sessions.inputs
    assert isinstance(record.receipt, InputDelivered)
    assert record.receipt.invocation == record.reserved_to
    reported = [t for t in sink.transitions if t.kind.value == "steer_consumed"]
    assert [(t.agent_kind, t.execution_id) for t in reported] == [
        ("skeleton-implementer", record.receipt.invocation.invocation_id.root)
    ]


async def test_a_run_with_no_steer_sends_the_turn_no_operator_input(tmp_path: Path) -> None:
    sent = Sent()
    channel = RuntimeRunControlChannel(FakeRunControlEventSink(), threads=SimThreads())
    with open_skeleton_world(tmp_path, agents=_agents(sent)) as world:
        core = await _run(world, channel)

    assert len(sent.messages) == 1
    assert "operator" not in sent.messages[0]
    assert core.sessions.inputs == ()


def test_a_steer_control_core_holds_without_its_input_gets_the_input_exactly_once() -> None:
    """A crash between the control and its input is repaired by the next poll, never doubled."""
    channel = RuntimeRunControlChannel(FakeRunControlEventSink(), threads=SimThreads())
    bridge = RunControlBridge(channel, _NoStore())
    ref = ArtifactRef(artifact_id=ArtifactId(root="steer-control-1"), digest="d" * 64)
    control = ControlInput(control_id=ControlId(root="control-1"), action="steer", artifact=ref)
    core = initial_state()
    core = core.model_copy(update={"run": core.run.model_copy(update={"controls": (control,)})})

    first = bridge.poll(core, now_at=1.0)

    received = [event for event in first if isinstance(event, SessionInputReceived)]
    assert [event.input.artifact for event in received] == [ref]
    recorded = core.model_copy(
        update={
            "sessions": core.sessions.model_copy(
                update={"inputs": tuple(InputRecord(input=e.input) for e in received)}
            )
        }
    )
    assert bridge.poll(recorded, now_at=2.0) == ()


class _NoStore:
    def put(self, ref: ArtifactRef, text: str) -> None:
        raise AssertionError((ref, text))
