"""An interrupt-mode steer reaches the turn that is running, through the production loop.

The run is the production loop over the real shell, the Fake Slurm cluster and a steerable
Fake provider: a provider profile that takes messages mid-turn (``FakeSteering``), which
passes the same session contract suite as the production sessions. The operator sends the
steer while the implementer's first turn runs; the attempt asks a second turn after it.

Core records the offer (``InputOfferedMidTurn``) before the provider is asked, and that the
provider took it (``InputTakenMidTurn``) after. Only a taken offer joins the turn; a refusal
rides in the turn's own result and sends the steer to the next turn. The host dies and
restarts at each boundary of that exchange: after the offer is recorded, after the provider
took the message, and after core recorded that it did. A host that dies after the offer leaves a
turn that ends before anyone asks the provider: the next host's ask finds no running turn,
and the steer goes to the next turn. Wherever it dies, the provider is asked only for offers
core has recorded, and the steer reaches the agent (delivery is at least once) with one
receipt, and with a report that says whether the running turn took it.

One order this harness does not produce: within one host, the turn's acceptance committing
after core records the offer and before the provider is asked. In production it is ordinary:
the loop commits the offer, then awaits while it publishes, and a turn whose provider thread
ends meanwhile has its acceptance committed in the same settle, before the next poll asks.
A simulated turn here ends only while the loop waits for it, so the order never arises. Core
alone decides what that order does (the offer goes back to waiting, for the next turn), and
the core suite pins it
(``test_a_turn_that_ends_before_the_provider_takes_its_offer_leaves_it_for_the_next`` in
``libs/vs-core/tests/test_mid_turn_inputs.py``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import pytest
from tests.support.session_world import ProviderFaults, SessionHost
from tests.support.skeleton_strategy import SkeletonConfig, SkeletonStrategy
from tests.support.skeleton_world import (
    IMPLEMENTATION,
    IMPLEMENTER,
    LEASE,
    CandidateWriter,
    Implementation,
    open_skeleton_world,
)
from tests.support.workspace_world import RUN_ID
from tests.system.steer.test_steer_reaches_the_next_turn import MessageResolver, Sent, StoredSteers

from vs_agent.api import AgentClient, SteerOutcome
from vs_agent.api.testing import (
    FakeAgentInvocationStore,
    FakeProvider,
    FakeSteering,
    FakeTurnScript,
)
from vs_core.api import InputDelivered, RunStatus, SessionsState
from vs_core.api.testing import sample_limits
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
from vs_runtime.api.infrastructure import RuntimeRunControlChannel, SteerMode
from vs_runtime.api.testing import FakeRunControlEventSink
from vs_sim.api.testing import ManualClock, clock_from, loop_threads

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests.support.skeleton_world import Process, World

    from vs_agent.api import AgentTurnRequest
    from vs_core.api import CoreState, InputRecord, MidTurnOffer
    from vs_project.api import (
        CommitOutcome,
        QuarantinedEnvelope,
        StateStore,
        StoredEnvelope,
        StoreFence,
        StoreRecord,
    )
    from vs_runtime.api.core import TurnSteering
    from vs_sim.api import Event

STEER = "switch the KV cache to fp8 now"
_GUARD_S = 600.0
"""Bounds only a turn whose release never comes; every scenario releases it."""


class Boundary(StrEnum):
    """Where the host dies, once, in the exchange of one interrupt-mode steer."""

    NONE = "none"
    AFTER_OFFER = "after-offer"
    """Core has recorded the offer; the provider has not been asked."""
    AFTER_CALL = "after-call"
    """The provider took the message into the running turn; core has not heard back."""
    AFTER_TAKEN = "after-taken"
    """Core has recorded that the provider took the message."""


class HostDiedError(RuntimeError):
    """The scripted death of one host process."""


def _sessions(envelope: StoredEnvelope) -> SessionsState:
    """Core's session records in a stored envelope, as the next host will read them."""
    payload = json.loads(envelope.payload)
    return SessionsState.model_validate_json(json.dumps(payload["envelope"]["core"]["sessions"]))


def _steer(sessions: SessionsState) -> InputRecord | None:
    return next(iter(sessions.inputs), None)


@dataclass
class Scenario:
    """One run: where it dies, what the provider was asked, and the turns' messages."""

    boundary: Boundary
    channel: RuntimeRunControlChannel
    sent: Sent = field(default_factory=Sent)
    asked: list[tuple[int, SteerOutcome]] = field(default_factory=list)
    """Every offer a provider was asked: which host asked, and the answer."""
    hosts: int = 0
    died: bool = False
    process: Process | None = None
    release: Event | None = None

    def core(self) -> CoreState:
        """What the running host's core holds now."""
        assert self.process is not None
        return self.process.shell.record.envelope.core

    def on_turn(self, writer: CandidateWriter, request: AgentTurnRequest) -> None:
        """The implementer's turn: the first one is steered by the operator while it runs."""
        first = not self.sent.messages
        self.sent.record(request)
        writer(request)
        if first:
            self.channel.queue_steer(STEER, mode=SteerMode.INTERRUPT)
            assert self.release is not None
            # The turn runs until the provider is asked for the steer or the host dies.
            assert self.release.wait(_GUARD_S)

    def died_at(self, envelope: StoredEnvelope) -> bool:
        """Whether this durable write is the one the host dies right after."""
        if self.died:
            return False
        steer = _steer(_sessions(envelope))
        if steer is None or steer.offered_to is None or steer.receipt is not None:
            return False
        taken = steer.taken_by is not None
        return (self.boundary is Boundary.AFTER_OFFER and not taken) or (
            self.boundary is Boundary.AFTER_TAKEN and taken
        )

    def die(self) -> HostDiedError:
        """The host dies here; the turn it held open is let go to end on its own."""
        self.died = True
        assert self.release is not None
        self.release.set()
        return HostDiedError(self.boundary.value)


@dataclass
class ObservedSteering:
    """The turn steering of one process: checks each ask, and dies after one if scripted."""

    inner: TurnSteering
    scenario: Scenario

    def offer_mid_turn(self, offer: MidTurnOffer) -> SteerOutcome:
        """Ask the provider, only for an offer core has recorded and settled nothing for."""
        [record] = [row for row in self.scenario.core().sessions.inputs if row.input == offer.input]
        assert record.offered_to == offer.invocation
        assert record.receipt is None
        outcome = self.inner.offer_mid_turn(offer)
        self.scenario.asked.append((self.scenario.hosts, outcome))
        if self.scenario.boundary is Boundary.AFTER_CALL and not self.scenario.died:
            raise self.scenario.die()
        assert self.scenario.release is not None
        self.scenario.release.set()
        return outcome


@dataclass
class DyingStore:
    """The run's state store; the host dies right after the write the scenario names."""

    inner: StateStore
    scenario: Scenario

    def observe(self) -> StoreRecord | None:
        """The inner store's atomic observation."""
        return self.inner.observe()

    def load(self) -> StoreRecord | None:
        """The inner store's record."""
        return self.inner.load()

    def commit(
        self, expected_revision: int | None, envelope: StoredEnvelope, fence: StoreFence, now: float
    ) -> CommitOutcome:
        """One durable write, visible to the next host even when this one dies after it."""
        outcome = self.inner.commit(expected_revision, envelope, fence, now)
        if self.scenario.died_at(envelope):
            raise self.scenario.die()
        return outcome

    def quarantine(
        self,
        expected_revision: int | None,
        envelope: QuarantinedEnvelope,
        fence: StoreFence,
        now: float,
    ) -> CommitOutcome:
        """The inner store's quarantine."""
        return self.inner.quarantine(expected_revision, envelope, fence, now)

    def acquire(self, host_id: str, now: float, duration: float) -> StoreFence | None:
        """The inner store's lease acquisition."""
        return self.inner.acquire(host_id, now, duration)

    def renew(self, fence: StoreFence, now: float, duration: float) -> StoreFence | None:
        """The inner store's lease renewal."""
        return self.inner.renew(fence, now, duration)

    def release(self, fence: StoreFence, now: float) -> bool:
        """The inner store's lease release."""
        return self.inner.release(fence, now)

    def verify(self, fence: StoreFence, now: float) -> bool:
        """The inner store's fence check."""
        return self.inner.verify(fence, now)

    def mutate_if_fenced(self, fence: StoreFence, now: float, mutation: Callable[[], None]) -> bool:
        """The inner store's fenced external mutation."""
        return self.inner.mutate_if_fenced(fence, now, mutation)


def _agents(scenario: Scenario, steering: FakeSteering) -> Callable[[Path], SessionHost]:
    def open_agents(root: Path) -> SessionHost:
        writer = CandidateWriter(root)
        threads = loop_threads()
        scenario.release = threads.event()
        provider = FakeProvider(
            script=FakeTurnScript((writer.answer,), steering=steering),
            on_turn=lambda request: scenario.on_turn(writer, request),
            threads=threads,
        )
        resolver = MessageResolver(
            root,
            TemplateRenderer(root),
            roles=frozenset({IMPLEMENTER}),
            schemas={IMPLEMENTATION: Implementation},
            writer=writer,
        )
        return SessionHost(
            resolver,
            AgentClient(provider, clock=ManualClock(), threads=threads),
            FakeAgentInvocationStore(),
            [],
            ProviderFaults(),
        )

    return open_agents


async def _run(world: World, scenario: Scenario) -> CoreState:
    """Run to the terminal status, starting a new host each time one dies."""
    artifacts = ArtifactStore(world.env.project.state.local_namespace(RUN_ID, "artifacts"))
    resolver = world.agents.resolver
    assert isinstance(resolver, MessageResolver)
    resolver.messages = TurnMessages(artifacts)
    world.store = DyingStore(world.env.project.state_store(RUN_ID), scenario)
    world.steering = lambda inner: ObservedSteering(inner, scenario)
    clock = clock_from(1.0)
    for _ in range(3):
        scenario.hosts += 1
        process = world.runtime()
        scenario.process = process
        host = CoreRunHost(
            process.shell,
            process.delivery,
            clock,
            RunControlBridge(scenario.channel, StoredSteers(artifacts)),
        )
        config = RunLoopConfig(
            host_id=f"steer-{scenario.hosts}",
            lease_duration=LEASE,
            control_poll_interval=1.0,
            max_dispatches=60,
        )
        if scenario.hosts > 1:
            # The runtime this test drives takes a lease, which a host that died keeps until it
            # expires: a restart waits it out on the run clock.
            await clock.sleep(LEASE)
        try:
            start_core(host, config)
            await drive_core(host, config)
        except HostDiedError:
            continue
        return process.shell.record.envelope.core
    pytest.fail("the run never finished")


_SCENARIOS = [
    (boundary, steering)
    for boundary in Boundary
    for steering in (FakeSteering.CONSUMED, FakeSteering.REJECTED)
]


@pytest.mark.parametrize(("boundary", "steering"), _SCENARIOS)
async def test_an_interrupt_steer_reaches_the_agent_wherever_the_host_dies(
    tmp_path: Path, boundary: Boundary, steering: FakeSteering
) -> None:
    sink = FakeRunControlEventSink()
    scenario = Scenario(boundary, RuntimeRunControlChannel(sink, threads=loop_threads()))
    strategy = SkeletonStrategy(config=SkeletonConfig(turns=2))
    with open_skeleton_world(
        tmp_path, strategy=strategy, agents=_agents(scenario, steering)
    ) as world:
        # Two implementer turns, as the attempt asks them, and a reconciliation bound that
        # outlasts the wait for a dead host's lease.
        world.limits = sample_limits(max_turns=4, reconciliation_bound=300.0)
        core = await _run(world, scenario)

    assert core.run.status == RunStatus.TERMINAL
    assert core.run.result is not None
    assert core.run.result.outcome == "success"
    assert scenario.died is (boundary is not Boundary.NONE)
    # The host that died after the offer had not asked the provider; the others had, and the
    # provider took the message into the running turn.
    first_host = [outcome for host, outcome in scenario.asked if host == 1]
    if boundary is Boundary.AFTER_OFFER:
        assert first_host == []
    else:
        assert first_host == [SteerOutcome.DELIVERED]
    # One input, settled once: delivered to a turn, never dropped, never left waiting.
    [record] = core.sessions.inputs
    assert isinstance(record.receipt, InputDelivered)
    in_messages = sum(message.count(STEER) for message in scenario.sent.messages)
    reports = [
        (t.kind.value, t.execution_id, t.text)
        for t in sink.transitions
        if t.kind.value in {"steer_consumed", "steer_delivered_mid_turn"}
    ]
    invocation = record.receipt.invocation.invocation_id.root
    if boundary is Boundary.NONE and steering is FakeSteering.CONSUMED:
        assert record.receipt.mid_turn
    if boundary is Boundary.AFTER_OFFER or steering is FakeSteering.REJECTED:
        # The turn never kept it: it ended before anyone asked, or refused it in its result.
        assert not record.receipt.mid_turn
    if record.receipt.mid_turn:
        # The running turn took it: no turn message carries it, and the operator hears so.
        # A host that died after core recorded the take may report it so too (at least once).
        assert boundary in (Boundary.NONE, Boundary.AFTER_TAKEN)
        assert in_messages == 0
        assert reports == [("steer_delivered_mid_turn", invocation, "")]
    else:
        # Refused, never asked, or its fate died with the host: the next turn's message
        # carries it, and the operator hears that the running turn did not keep it.
        assert in_messages == 1
        assert STEER in scenario.sent.messages[-1]
        [(kind, execution, note)] = reports
        assert (kind, execution) == ("steer_consumed", invocation)
        assert "reached the next turn" in note
