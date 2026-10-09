"""Apply the quota policy when a provider has no capacity left."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from vs_agent.api import Attribution, ProviderSwitch, QuotaPlan
from vs_runtime._fallback import ProviderFallback
from vs_runtime._quota_policy import (
    PAUSE_FOR_OPERATOR,
    GiveUp,
    QuotaPolicy,
    Switch,
    WaitFor,
    decide_quota,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import NoReturn

    from vs_agent.api import (
        AgentEventSink,
        AgentQuotaError,
        AgentTurnRequest,
        ProviderSwitchReason,
    )
    from vs_runtime._run_control import RunControlChannel


class CapacityTimer(Protocol):
    """Time for the capacity gate: the current epoch second and a bounded park."""

    def now(self) -> float:
        """Seconds since the epoch, the timeline provider reset times are on."""
        ...

    def wait_resumed(self, seconds: float) -> bool:
        """Park the paused run for at most ``seconds``; True when something resumed it first.

        A wait that times out resumes the run itself. A stop request raises.
        """
        ...


class ControlCapacityTimer:
    """Production timer: the wall clock, and the run control channel's own bounded park."""

    def __init__(self, control: RunControlChannel, now: Callable[[], float]) -> None:
        """Bind the timer to the channel that owns the pause and to a clock."""
        self._control = control
        self._now = now

    def now(self) -> float:
        """Seconds since the epoch."""
        return self._now()

    def wait_resumed(self, seconds: float) -> bool:
        """Park on the control channel for at most ``seconds``."""
        return self._control.wait_resumed(seconds)


@dataclass(frozen=True, slots=True)
class CapacityHandling:
    """How a run handles provider capacity limits, shared by every session it opens.

    ``fallback`` records which providers the run has replaced and is the one place
    sessions consult when they open. A ``None`` timer is the production one, built
    from the run's control channel.
    """

    policy: QuotaPolicy = PAUSE_FOR_OPERATOR
    timer: CapacityTimer | None = None
    fallback: ProviderFallback = field(default_factory=lambda: ProviderFallback(None))

    @classmethod
    def for_policy(
        cls, policy: QuotaPolicy, timer: CapacityTimer | None = None
    ) -> CapacityHandling:
        """Handling for ``policy``, with a fallback state holding its target."""
        return cls(policy, timer, ProviderFallback(policy.fallback))


PAUSE_ONLY = CapacityHandling()
"""Pause for the operator, with the production timer and no fallback."""


class PolicyCapacityGate:
    """Turn a provider capacity limit into a run pause that the policy or the operator ends.

    The run's existing cooperative pause carries the state: the turn that hit
    the limit stays in flight on its worker thread, the run reports PAUSING and
    then PAUSED, and the typed ``quota_paused`` event says why. Depending on the
    policy the run waits for the operator, resumes itself when capacity should
    have returned, or gives up and lets the turn fail with the quota error.
    After a resume the same turn is sent again, so a run that waited and resumed
    on its provider has the same experiment state as one that never stopped. A
    stop request ends the wait by raising ``RunStopped``.

    A switch to the fallback provider (by the policy once its wait is spent, or at
    the operator's request) is recorded in the run's ``ProviderFallback`` and ends
    the turn in flight with the quota error: a session cannot change provider, so
    that turn's hypothesis stops here and sessions opened afterwards run on the
    fallback. A session still open on the replaced provider fails its next
    capacity stop at once.
    """

    def __init__(
        self,
        control: RunControlChannel,
        events: AgentEventSink,
        handling: CapacityHandling,
        timer: CapacityTimer,
    ) -> None:
        """Bind the gate to the run's control channel, event sink, handling and timer."""
        self._control = control
        self._events = events
        self._policy = handling.policy
        self._fallback = handling.fallback
        self._timer = timer
        self._waited = 0.0

    def wait_for_capacity(
        self, error: AgentQuotaError, turn: AgentTurnRequest, *, role: str, attempt: int
    ) -> None:
        """Pause the run, publish why, and return once the turn may be sent again."""
        context = Attribution(role, turn.label, turn.invocation_id)
        if attempt == 1:
            self._waited = 0.0
        if self._fallback.replaced(error.provider):
            self._abandon(
                error,
                f"the run switched away from {error.provider}; this session cannot continue on it",
                context,
            )
        now = self._timer.now()
        decision = decide_quota(
            self._policy, resets_at=error.resets_at, now=now, waited=self._waited
        )
        if isinstance(decision, GiveUp):
            self._abandon(error, decision.reason, context)
        if isinstance(decision, Switch):
            self._switch_or_abandon(error, "policy", decision.reason, context)
        resumes_at = now + decision.seconds if isinstance(decision, WaitFor) else None
        target = self._fallback.target
        plan = QuotaPlan(
            self._policy.action.value,
            resumes_at,
            None if target is None else target.provider,
            None if target is None else target.model,
        )
        self._events.quota_paused(error, plan, context)
        self._control.request_pause()
        if isinstance(decision, WaitFor):
            by_operator = self._timer.wait_resumed(decision.seconds)
            self._waited += self._timer.now() - now
        else:
            self._control.wait_while_paused()
            by_operator = True
        if self._control.consume_fallback_request() and self._fallback.target is not None:
            self._switch_or_abandon(
                error, "operator", "the operator resumed with the fallback", context
            )
        self._events.quota_resumed(
            error.provider, "operator" if by_operator else "wait_elapsed", context
        )

    def _switch_or_abandon(
        self,
        error: AgentQuotaError,
        reason: ProviderSwitchReason,
        detail: str,
        context: Attribution,
    ) -> NoReturn:
        """Replace the provider by the fallback when there is one, then end the turn."""
        target = self._fallback.target
        if target is None or target.provider == error.provider:
            self._abandon(error, f"no fallback to switch to ({detail})", context)
        if self._fallback.replace_provider(error.provider):
            self._events.provider_switched(
                ProviderSwitch(error.provider, target.provider, target.model, reason, detail),
                context,
            )
        self._abandon(
            error,
            f"switched to {target.provider} ({target.model}); the turn cannot move to another provider",
            context,
        )

    def _abandon(self, error: AgentQuotaError, reason: str, context: Attribution) -> NoReturn:
        """End the turn in flight with the quota error."""
        self._events.quota_abandoned(error, reason, context)
        raise error
