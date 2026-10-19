"""A provider outage shorter than the outage budget never ends a run (S179).

A gateway answered ``503 No available accounts`` for about two hours. Each planner turn sat in
the capacity gate, which waits out an outage for up to six hours, but core charged that wait to
the planner's 30 minute turn limit, cancelled the turn, recorded it lost, and after three lost
planner turns the dynamic planner reported itself stuck and the run ended with no winner.

The dynamic search runs on the production shell and loop over one virtual clock. Each agent
turn runs a real ``AgentClient`` behind the production capacity gate (``install_capacity_gate``,
the production timer on simulated threads), sharing the run's control channel with the loop's
``RunControlBridge`` as the host wires them. The Fake provider takes ``send_s`` virtual seconds
per send (agentshim's own transient retry, 30 s, and the attempts) and fails a send that ends
while the outage is down; a cancel ends a send at once, as stopping the provider's process does. A turn the client finishes gets the scripted reply of its role; a client failure is
what the session executor reports for one (an unknown outcome), and a cancelled flight raises
as it does in production. Everything else is answered at once by the dynamic script.

The property: whatever the outage pattern (one window, several windows, or a route that serves
some sends), its length below the budget, and the turn it hits (planner, implementer, judge,
the planner's resumed second turn), the run ends with a winner, the gate never gives a turn up,
and no turn is asked twice.
"""

from __future__ import annotations

import asyncio
import enum
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import Phase, example, given, settings
from hypothesis import strategies as st
from tests.support.dynamic_script import Executors, config, harness
from tests.vibesys.orchestration.dynamic._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)

from vibesys.orchestration.dynamic.search.api import dynamic
from vs_agent.api import (
    AgentClient,
    AgentQuotaError,
    AgentSessionKey,
    AgentTurnRequest,
    Attribution,
    NullAgentEventSink,
    QuotaCondition,
    QuotaPlan,
    SessionScope,
)
from vs_agent.api.testing import FakeProvider, FakeTurnScript
from vs_core.api import CancelTurn, DispatchTurn, ResumeSessionTurn, RunStatus
from vs_core.testing.drive import Unknown, new_run
from vs_project.api import FakeStateStore
from vs_runtime.api.core import PRODUCTION_LEASE_SECONDS as LEASE_SECONDS
from vs_runtime.api.core import (
    CoreRunHost,
    CoreRuntime,
    CoreRuntimeBindings,
    RunControlBridge,
    RunLoopConfig,
    drive_core,
    start_core,
)
from vs_runtime.api.infrastructure import (
    PROVIDER_OUTAGE,
    CapacityHandling,
    QuotaAction,
    QuotaPolicy,
    RuntimeRunControlChannel,
    install_capacity_gate,
)
from vs_runtime.api.testing import (
    FakePublicationDelivery,
    ScriptedExecutors,
    request_executors,
)
from vs_sim.api.testing import (
    ManualClock,
    SimBlockingRunner,
    VirtualClock,
    examples,
    loop_threads,
    run_virtual,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel

    from vs_core.api import CoreState, OperationRegistry, Request, SchemaRef
    from vs_core.testing.drive import Answer
    from vs_runtime.api.core import ExecutionContext, ExecutionResult
    from vs_sim.api.testing import SimThreads

START = 1.0
BUDGET = PROVIDER_OUTAGE.budget_seconds
PROVIDER = "claude"
_CONTROL_POLL_S = 60.0
"""How often the loop reads its controls; production reads them every second."""
_WORK_S = 30.0
"""How long a send takes once the provider serves it."""

# A run costs host seconds, so a pull request replays the pinned examples and a few drawn
# ones; the nightly depth draws more.
_SETTINGS = settings(
    max_examples=examples(pr=4, nightly=40),
    phases=(Phase.explicit, Phase.generate),
    deadline=None,
    derandomize=True,
    database=None,
)


class Pattern(enum.StrEnum):
    """How the outage looks from the run."""

    TOTAL = "total"
    """Down from the hit turn's first send for the whole length."""
    INTERMITTENT = "intermittent"
    """The length split into windows with short gaps that serve a send."""
    PARTIAL = "partial"
    """Down for the whole length, but every third send is served (the route serves some)."""


class Hit(enum.StrEnum):
    """The turn whose first send starts the outage."""

    PLANNER = "planner"
    IMPLEMENTER = "implementer"
    JUDGE = "judge"
    RESUMED_PLANNER = "resumed-planner"
    """The planner's second turn, which resumes its session."""


_TURN_TIME = {"orchestrator": 1800.0, "implementer": 7200.0, "judge": 1800.0}
"""Each role's ``turn_time`` in the dynamic search (planner and judge 30 min, implementer 2 h)."""


@dataclass
class Outage:
    """When the provider is down, anchored at the hit turn's first send.

    With ``left`` the hit turn first works until it has ``left`` seconds of its own time left,
    and the outage starts then; else it starts with the hit turn's first send.
    """

    pattern: Pattern
    hit: Hit
    length: float
    gap: float = 600.0
    left: float | None = None
    started_at: float | None = None
    planner_turns: int = 0
    sends: int = 0

    def anchor(self, role: str, now: float) -> None:
        """Start the outage at the first send of the hit turn."""
        if self.started_at is not None:
            return
        if role == "orchestrator":
            self.planner_turns += 1
        wanted = {
            Hit.PLANNER: role == "orchestrator",
            Hit.IMPLEMENTER: role == "implementer",
            Hit.JUDGE: role == "judge",
            Hit.RESUMED_PLANNER: role == "orchestrator" and self.planner_turns == 2,
        }[self.hit]
        if wanted:
            lead = 0.0 if self.left is None else max(_TURN_TIME[role] - self.left, 0.0)
            self.started_at = now + lead

    def down(self, at: float) -> bool:
        """Whether a send that ends at ``at`` fails."""
        self.sends += 1
        if self.started_at is None:
            return False
        into = at - self.started_at
        match self.pattern:
            case Pattern.TOTAL:
                return 0.0 <= into < self.length
            case Pattern.PARTIAL:
                return 0.0 <= into < self.length and self.sends % 3 != 0
            case Pattern.INTERMITTENT:
                # Three down windows of a third of the length, each followed by a gap.
                window = self.length / 3
                if into < 0.0 or into >= 3 * (window + self.gap):
                    return False
                return into % (window + self.gap) < window


class _Sink(NullAgentEventSink):
    def __init__(self) -> None:
        self.abandoned: list[str] = []

    def quota_paused(self, error: AgentQuotaError, plan: QuotaPlan, where: Attribution) -> None:
        del error, plan, where

    def quota_resumed(
        self, error: AgentQuotaError, reason: str, waited_seconds: float, where: Attribution
    ) -> None:
        del error, reason, waited_seconds, where

    def quota_abandoned(
        self, error: AgentQuotaError, reason: str, waited_seconds: float, where: Attribution
    ) -> None:
        del error, waited_seconds, where
        self.abandoned.append(reason)


@dataclass
class _Provider:
    """What every turn's agent client shares: the run's channel, capacity handling and clock."""

    clock: VirtualClock
    threads: SimThreads
    channel: RuntimeRunControlChannel
    handling: CapacityHandling
    outage: Outage
    send_s: float
    sink: _Sink = field(default_factory=_Sink)
    asked: list[str] = field(default_factory=list)
    """The role of each turn the run asked, in order."""

    def client(self, role: str) -> AgentClient:
        """A fresh client for one turn of ``role``, behind the run's capacity gate."""

        def send(request: AgentTurnRequest) -> None:
            del request
            self.outage.anchor(role, self.clock.now())
            if self.outage.started_at is None:
                # A cancel ends a send at once, as the provider's process is stopped.
                provider.hold_until_cancelled(_WORK_S)
                return
            # The hit turn's first send works until the outage starts, then fails as any does.
            provider.hold_until_cancelled(
                max(self.outage.started_at - self.clock.now(), 0.0) + self.send_s
            )
            if self.outage.down(self.clock.now()):
                raise AgentQuotaError(
                    PROVIDER, QuotaCondition.UNAVAILABLE, "API Error: 503 No available accounts"
                )

        provider = FakeProvider(script=FakeTurnScript(("ok",)), on_turn=send, threads=self.threads)
        client = AgentClient(
            provider,
            provider=PROVIDER,
            model_name="m",
            clock=ManualClock(START),
            threads=self.threads,
        )
        install_capacity_gate(
            client, control=self.channel, events=self.sink, handling=self.handling, clock=self.clock
        )
        return client


class _Script:
    """The dynamic script, except for a turn the client failed: its outcome is unknown."""

    def __init__(self, inner: Executors) -> None:
        self._inner = inner
        self.failed: set[str] = set()

    def __call__(self, request: Request, core: CoreState) -> Answer:
        if request.request_id is not None and request.request_id.root in self.failed:
            return Unknown()
        return self._inner(request, core)


class _GatedExecutors(ScriptedExecutors):
    """Agent turns run the real client behind the gate; every other request is scripted."""

    def __init__(
        self,
        script: _Script,
        core: Callable[[], CoreState],
        registry: OperationRegistry,
        schemas: Mapping[SchemaRef, type[BaseModel]],
        provider: _Provider,
    ) -> None:
        super().__init__(script, core, registry, schemas)
        self._failed = script.failed
        self._provider = provider
        self._blocking = SimBlockingRunner(provider.threads)
        self._running: dict[str, tuple[AgentClient, str]] = {}
        self._stopping: set[asyncio.Future[None]] = set()

    async def execute(self, request: Request, context: ExecutionContext) -> ExecutionResult:
        if isinstance(request, CancelTurn):
            running = self._running.get(request.invocation.invocation_id.root)
            if running is not None:
                # The provider takes the cancel and ends the turn on its own thread; the
                # request that asked for it is answered at once.
                self._stopping.add(asyncio.ensure_future(self._stop(*running)))
            return await super().execute(request, context)
        if not isinstance(request, DispatchTurn | ResumeSessionTurn):
            return await super().execute(request, context)
        assert request.request_id is not None
        role = request.turn.session.role_id.root.removeprefix("dynamic-")
        self._provider.asked.append(role)
        invocation = request.turn.invocation_id.root
        client = self._provider.client(role)
        self._running[invocation] = (client, role)
        operation = asyncio.ensure_future(
            self._blocking.run(
                client.invoke_text,
                kind=role,
                workspace=Path(),
                system_prompt="Work.",
                user_prompt="work",
                round_label=f"{role}-turn",
                invocation_id=invocation,
            )
        )
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            # The loop cancelled the flight (core ended the turn): stop the provider turn, as
            # the session executor does, and let the cancellation go on.
            await self._stop(client, role)
            await asyncio.gather(operation, return_exceptions=True)
            raise
        except (RuntimeError, TimeoutError):
            # The session executor reports a turn the client failed as of unknown outcome.
            self._failed.add(request.request_id.root)
        finally:
            self._running.pop(invocation, None)
        return await super().execute(request, context)

    async def _stop(self, client: AgentClient, role: str) -> None:
        await self._blocking.run(client.cancel_session, AgentSessionKey(SessionScope.ROLE, role))


def _ignore(transition: object) -> None:
    del transition


@dataclass(frozen=True)
class Played:
    """What one run did."""

    core: CoreState
    asked: Counter[str]
    abandoned: list[str]
    outage_started: bool


def play(outage: Outage, *, send_s: float, policy: QuotaPolicy, rounds: int = 1) -> Played:
    """One dynamic run of ``rounds`` planning calls through ``outage``."""
    clock = VirtualClock(START)
    script = Executors(
        planner=deque(plan_reply(implement(f"h{n}")) for n in range(rounds)),
        implementer=deque(implemented() for _ in range(rounds)),
        judge=deque(reviewed() for _ in range(rounds)),
    )
    selected = config(max_in_flight=1, max_rounds=rounds)
    hosting = harness(selected)
    search = dynamic.driver(selected, hosting.registry)

    async def main() -> Played:
        threads = loop_threads()
        channel = RuntimeRunControlChannel(_ignore, threads=threads)
        provider = _Provider(
            clock, threads, channel, CapacityHandling.for_policy(policy, threads), outage, send_s
        )
        shell: CoreRuntime[Any] = CoreRuntime(
            FakeStateStore(),
            search,
            new_run(search, hosting),
            config=selected,
            bindings=CoreRuntimeBindings(
                registry=hosting.registry,
                executors=request_executors(
                    _GatedExecutors(
                        _Script(script),
                        lambda: shell.record.envelope.core,
                        hosting.registry,
                        dynamic.reply_schemas,
                        provider,
                    )
                ),
            ),
        )
        host = CoreRunHost(
            shell, FakePublicationDelivery(), clock, RunControlBridge(channel, _Steers())
        )
        loop_config = RunLoopConfig(
            host_id="outage",
            lease_duration=LEASE_SECONDS,
            control_poll_interval=_CONTROL_POLL_S,
            max_dispatches=400,
        )
        start_core(host, loop_config)
        await drive_core(host, loop_config)
        return Played(
            shell.record.envelope.core,
            Counter(provider.asked),
            provider.sink.abandoned,
            outage.started_at is not None,
        )

    return run_virtual(clock, main())


class _Steers:
    def put(self, ref: object, text: str) -> None:
        del ref, text


_POLICIES = st.sampled_from(
    [
        QuotaPolicy(QuotaAction.PAUSE),
        QuotaPolicy(QuotaAction.WAIT, wait_seconds=7200.0, retry_seconds=300.0),
    ]
)


@pytest.mark.regression
@_SETTINGS
@example(
    pattern=Pattern.TOTAL,
    hit=Hit.PLANNER,
    length=2 * 3600.0,
    send_s=60.0,
    policy=QuotaPolicy(QuotaAction.WAIT, wait_seconds=7200.0, retry_seconds=300.0),
)
@example(
    pattern=Pattern.TOTAL,
    hit=Hit.IMPLEMENTER,
    length=BUDGET - 3600.0,
    send_s=30.0,
    policy=QuotaPolicy(QuotaAction.PAUSE),
)
@given(
    pattern=st.sampled_from(Pattern),
    hit=st.sampled_from(Hit),
    length=st.floats(min_value=60.0, max_value=BUDGET - 3600.0),
    send_s=st.floats(min_value=5.0, max_value=600.0),
    policy=_POLICIES,
)
def test_an_outage_shorter_than_the_budget_never_ends_the_run(
    pattern: Pattern, hit: Hit, length: float, send_s: float, policy: QuotaPolicy
) -> None:
    rounds = 2 if hit is Hit.RESUMED_PLANNER else 1
    played = play(Outage(pattern, hit, length), send_s=send_s, policy=policy, rounds=rounds)

    assert played.outage_started, "the outage hit no turn"
    assert played.abandoned == [], "the gate gave a turn up"
    assert played.core.run.status == RunStatus.TERMINAL
    assert played.core.run.result is not None
    assert played.core.run.result.outcome == "success", played.core.run.result
    # No turn was lost and asked again: one turn per role per round.
    assert played.asked == Counter(orchestrator=rounds, implementer=rounds, judge=rounds)


@pytest.mark.regression
@_SETTINGS
# The resumed planner turn near its limit (review of #3283, MED-1): 300 s and 200 s left.
@example(
    hit=Hit.RESUMED_PLANNER,
    spare=60.0,
    length=3600.0,
    send_s=240.0,
    policy=QuotaPolicy(QuotaAction.WAIT, wait_seconds=7200.0, retry_seconds=300.0),
)
@example(
    hit=Hit.JUDGE, spare=50.0, length=3600.0, send_s=150.0, policy=QuotaPolicy(QuotaAction.PAUSE)
)
@given(
    hit=st.sampled_from(Hit),
    # The gate's report reaches core at the loop's next control poll, so a first stop must
    # reach the gate one poll before the limit (production polls each second).
    spare=st.floats(min_value=_CONTROL_POLL_S, max_value=1800.0),
    length=st.floats(min_value=60.0, max_value=BUDGET - 3600.0),
    send_s=st.floats(min_value=5.0, max_value=600.0),
    policy=_POLICIES,
)
def test_an_outage_late_in_a_turn_never_ends_the_run(
    hit: Hit, spare: float, length: float, send_s: float, policy: QuotaPolicy
) -> None:
    """Wherever in the hit turn the outage starts, the run still ends with a winner.

    The hit turn works until ``send_s + spare`` of its own time is left, then the outage
    starts, so its first failing send reaches the gate with ``spare`` left (at least one
    control poll, for its report to reach core before the limit). Before (review of
    #3283, MED-1), the resend after the gate's first wait was charged to the turn, which core
    cut as ``TRANSPORT_LOST``; a resumed turn is never asked again, and the run failed.
    """
    rounds = 2 if hit is Hit.RESUMED_PLANNER else 1
    outage = Outage(Pattern.TOTAL, hit, length, left=send_s + spare)
    played = play(outage, send_s=send_s, policy=policy, rounds=rounds)

    assert played.outage_started, "the outage hit no turn"
    assert played.abandoned == [], "the gate gave a turn up"
    assert played.core.run.result is not None
    assert played.core.run.result.outcome == "success", played.core.run.result
    assert played.asked == Counter(orchestrator=rounds, implementer=rounds, judge=rounds)


def test_a_run_with_no_outage_asks_each_role_once() -> None:
    """The harness itself: with the provider up, the run succeeds and asks each role once."""
    played = play(
        Outage(Pattern.TOTAL, Hit.PLANNER, 0.0),
        send_s=30.0,
        policy=QuotaPolicy(QuotaAction.PAUSE),
    )
    assert played.core.run.result is not None
    assert played.core.run.result.outcome == "success", played.core.run.result
    assert played.asked == Counter(orchestrator=1, implementer=1, judge=1)
