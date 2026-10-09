"""A run that exhausts its provider switches to the fallback at a session boundary.

The scripted client is built per session from the spec the session opens with, so
the provider and model a session actually ran on are observable in the lifecycle
events, as they are for a real run. The switch is read from the events the run
published, and the operator is simulated from the control channel's own PAUSED
transition. Nothing sleeps or reads real time.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.runtime_agent_sessions import (
    _ClientFactory,
    _environment,
    _EnvironmentOpener,
    _runtime,
    _RuntimeEffects,
)

from vs_agent.api import (
    AgentBackend,
    AgentCapabilities,
    AgentQuotaError,
    AgentSpec,
    Attribution,
    NullAgentEventSink,
    ProviderSwitch,
    QuotaCondition,
    QuotaPlan,
)
from vs_agent.api.testing import FakeAgentClient
from vs_runtime.api import AgentRole, AgentSession
from vs_runtime.api.infrastructure import (
    AgentExecutionConfiguration,
    AgentExecutionStarted,
    CapacityHandling,
    FallbackTarget,
    ProviderFallback,
    QuotaAction,
    QuotaPolicy,
    RunControlChannel,
    RunControlTransition,
    RunControlTransitionKind,
    create_run_control_channel,
)
from vs_runtime.api.testing import (
    FakeAgentExecutionLifecycleSink,
    FakeCapacityTimer,
    FakeRunControlEventSink,
)

if TYPE_CHECKING:
    from vs_runtime.api.infrastructure import WorkspaceRuntime

ROLE = AgentRole(id="worker", system_prompt="Work.")
FALLBACK = FallbackTarget("codex", "gpt-fallback")
PRIMARY = AgentSpec(backend=AgentBackend.STUB, provider="claude", model="opus")
QUOTA = AgentQuotaError("claude", QuotaCondition.QUOTA_EXHAUSTED, "limit")


class _Sink(NullAgentEventSink):
    def __init__(self) -> None:
        self.paused: list[QuotaPlan] = []
        self.abandoned: list[str] = []
        self.switched: list[ProviderSwitch] = []

    def quota_paused(self, error: AgentQuotaError, plan: QuotaPlan, where: Attribution) -> None:
        del error, where
        self.paused.append(plan)

    def quota_abandoned(self, error: AgentQuotaError, reason: str, where: Attribution) -> None:
        del error, where
        self.abandoned.append(reason)

    def provider_switched(self, switch: ProviderSwitch, where: Attribution) -> None:
        del where
        self.switched.append(switch)


class _PerSpecClients(_ClientFactory):
    """Build each session's client from its spec; the primary provider is out of quota."""

    def __init__(self, primary_stops: int | None) -> None:
        super().__init__()
        self._primary_stops = primary_stops

    def __call__(self, **kwargs: object) -> FakeAgentClient:
        spec = kwargs["spec"]
        assert isinstance(spec, AgentSpec)
        client = FakeAgentClient(
            provider=spec.provider,
            model=spec.model,
            capabilities=AgentCapabilities(session_reuse=True),
        )
        client.set_text(None, f"answer from {spec.provider}")
        if spec.provider == PRIMARY.provider:
            client.fail("worker", QUOTA, times=self._primary_stops)
        return client


@dataclass
class _Outcomes:
    """What three turns saw: one on the first session, one on a fresh session, one stale."""

    first: str | AgentQuotaError
    fresh: str | AgentQuotaError
    stale: str | AgentQuotaError


@dataclass
class _World:
    policy: QuotaPolicy
    operator: str = "resume"
    primary_stops: int | None = None
    sink: _Sink = field(default_factory=_Sink)
    lifecycle: FakeAgentExecutionLifecycleSink = field(
        default_factory=FakeAgentExecutionLifecycleSink
    )
    transitions: list[RunControlTransition] = field(default_factory=list)
    timer: FakeCapacityTimer | None = None
    control: RunControlChannel | None = None

    def kinds(self) -> list[RunControlTransitionKind]:
        return [transition.kind for transition in self.transitions]

    def _on_transition(self, transition: RunControlTransition) -> None:
        self.transitions.append(transition)
        if transition.kind is not RunControlTransitionKind.PAUSED or self.timer is not None:
            return
        assert self.control is not None
        if self.operator == "fallback":
            self.control.resume_with_fallback()
        else:
            self.control.resume()

    def started(self) -> list[tuple[str | None, str | None]]:
        """The provider and model of every provider invocation, in order."""
        return [
            (event.provider, event.model)
            for event in self.lifecycle.events
            if isinstance(event, AgentExecutionStarted)
        ]

    def play(self) -> _Outcomes:
        control = create_run_control_channel(
            FakeRunControlEventSink(on_transition=self._on_transition)
        )
        self.control = control
        if self.policy.action in (QuotaAction.WAIT, QuotaAction.FALLBACK):
            self.timer = FakeCapacityTimer(control)
        handling = CapacityHandling.for_policy(self.policy, self.timer)
        outcomes: list[str | AgentQuotaError] = []

        async def turn(session: AgentSession, prompt: str) -> None:
            try:
                outcomes.append(str(await session.turn(prompt)))
            except AgentQuotaError as error:
                outcomes.append(error)

        async def run() -> None:
            runtime: WorkspaceRuntime = _runtime(
                ROLE,
                _RuntimeEffects(
                    _PerSpecClients(self.primary_stops),
                    _EnvironmentOpener(*[_environment() for _ in range(4)]),
                    self.lifecycle,
                    agent_events=self.sink,
                    capacity=handling,
                    spec=PRIMARY,
                ),
                control=control,
            )
            try:
                first = await runtime.agents.create_session(ROLE, workspace=runtime.workspaces.root)
                await turn(first, "one")
                fresh = await runtime.agents.create_session(ROLE, workspace=runtime.workspaces.root)
                await turn(fresh, "two")
                await turn(first, "three")
            finally:
                await runtime.workspaces.close()

        asyncio.run(run())
        return _Outcomes(*outcomes)


def test_a_fallback_policy_ends_the_turn_and_runs_the_next_session_on_the_fallback() -> None:
    world = _World(QuotaPolicy(QuotaAction.FALLBACK, fallback=FALLBACK))

    outcomes = world.play()

    assert isinstance(outcomes.first, AgentQuotaError)
    assert outcomes.fresh == "answer from codex"
    # The journal says who ran what: the stopped turn on claude, the fresh session on the fallback.
    assert world.started() == [("claude", "opus"), ("codex", "gpt-fallback"), ("claude", "opus")]
    [switch] = world.sink.switched
    assert (switch.from_provider, switch.to_provider, switch.to_model, switch.reason) == (
        "claude",
        "codex",
        "gpt-fallback",
        "policy",
    )
    assert world.transitions == []  # nothing waited, so the run never paused


def test_a_session_still_open_on_the_replaced_provider_fails_its_next_stop_at_once() -> None:
    world = _World(QuotaPolicy(QuotaAction.FALLBACK, fallback=FALLBACK))

    outcomes = world.play()

    assert isinstance(outcomes.stale, AgentQuotaError)
    assert len(world.sink.switched) == 1  # the replacement is recorded once, not per stop
    assert world.sink.paused == []
    assert any("switched away from claude" in reason for reason in world.sink.abandoned)


def test_an_operator_who_resumes_with_the_fallback_switches_the_run() -> None:
    world = _World(QuotaPolicy(fallback=FALLBACK), operator="fallback")

    outcomes = world.play()

    assert isinstance(outcomes.first, AgentQuotaError)
    assert outcomes.fresh == "answer from codex"
    [plan] = world.sink.paused
    assert (plan.policy, plan.fallback_provider, plan.fallback_model) == (
        "pause",
        "codex",
        "gpt-fallback",
    )
    [switch] = world.sink.switched
    assert switch.reason == "operator"
    assert world.kinds().count(RunControlTransitionKind.PAUSED) == 1


def test_an_operator_who_resumes_normally_stays_on_the_provider() -> None:
    world = _World(QuotaPolicy(fallback=FALLBACK), operator="resume", primary_stops=1)

    outcomes = world.play()

    assert outcomes.first == "answer from claude"
    assert world.sink.switched == []


def test_resuming_with_the_fallback_when_none_is_configured_just_resumes() -> None:
    world = _World(QuotaPolicy(), operator="fallback", primary_stops=1)

    outcomes = world.play()

    assert outcomes.first == "answer from claude"
    assert world.sink.switched == []


@given(
    stops=st.integers(min_value=1, max_value=8),
    retry=st.integers(min_value=1, max_value=50),
    budget=st.integers(min_value=1, max_value=200),
)
def test_a_fallback_waits_out_its_budget_then_switches_and_never_before(
    stops: int, retry: int, budget: int
) -> None:
    policy = QuotaPolicy(
        QuotaAction.FALLBACK, wait_seconds=budget, retry_seconds=retry, fallback=FALLBACK
    )
    world = _World(policy, primary_stops=stops)
    allowed_waits = -(-budget // retry)

    outcomes = world.play()

    assert world.timer is not None
    first_turn_waits = world.timer.waits[: min(stops, allowed_waits)]
    assert len(first_turn_waits) == min(stops, allowed_waits)
    assert sum(first_turn_waits) <= budget
    if stops <= allowed_waits:
        assert outcomes.first == "answer from claude"
        assert world.sink.switched == []
    else:
        assert isinstance(outcomes.first, AgentQuotaError)
        assert len(world.sink.switched) == 1


# --- the substitution itself -------------------------------------------------

providers = st.sampled_from(["claude", "codex", "gemini", "opencode"])


@given(
    provider=providers,
    replaced=st.sets(providers),
    model=st.none() | st.text(min_size=1, max_size=8),
    role_models=st.dictionaries(st.text(min_size=1, max_size=4), st.text(min_size=1, max_size=4)),
)
def test_only_a_replaced_providers_sessions_take_the_fallback_provider_and_model(
    provider: str, replaced: set[str], model: str | None, role_models: dict[str, str]
) -> None:
    fallback = ProviderFallback(FALLBACK)
    for name in replaced - {FALLBACK.provider}:
        fallback.replace_provider(name)
    original = AgentExecutionConfiguration(
        agent_id="worker",
        spec=AgentSpec(
            backend=AgentBackend.STUB,
            provider=provider,
            model=model,
            role_models=role_models,
            reasoning_effort="high",
        ),
        reasoning_effort="high",
    )

    applied = fallback.apply(original)

    if provider in replaced and provider != FALLBACK.provider:
        assert (applied.spec.provider, applied.spec.model) == (FALLBACK.provider, FALLBACK.model)
        assert applied.spec.role_models == {}
        assert applied.reasoning_effort is None
    else:
        assert applied == original


def test_replacing_a_provider_is_idempotent_and_reports_only_the_first_switch() -> None:
    fallback = ProviderFallback(FALLBACK)

    assert fallback.replace_provider("claude") is True
    assert fallback.replace_provider("claude") is False
    assert fallback.replaced("claude")
    assert not fallback.replaced("gemini")


@pytest.mark.parametrize(
    ("target", "provider"), [(None, "claude"), (FALLBACK, FALLBACK.provider)], ids=["none", "self"]
)
def test_a_switch_that_could_not_change_anything_is_refused(
    target: FallbackTarget | None, provider: str
) -> None:
    with pytest.raises(ValueError, match="fallback"):
        ProviderFallback(target).replace_provider(provider)


def test_a_fallback_target_names_both_a_provider_and_a_model() -> None:
    with pytest.raises(ValueError, match="provider and a model"):
        FallbackTarget("codex", "")
