"""Apply the quota policy when a provider has no capacity left."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vs_runtime._quota_policy import (
    PAUSE_FOR_OPERATOR,
    GiveUp,
    QuotaPolicy,
    WaitFor,
    decide_quota,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_agent.api import AgentEventSink, AgentQuotaError, AgentTurnRequest
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
    """How a run handles provider capacity limits: its policy and, in tests, its timer.

    A ``None`` timer is the production one, built from the run's control channel.
    """

    policy: QuotaPolicy = PAUSE_FOR_OPERATOR
    timer: CapacityTimer | None = None


PAUSE_ONLY = CapacityHandling()
"""Pause for the operator, with the production timer."""


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
    """

    def __init__(
        self,
        control: RunControlChannel,
        events: AgentEventSink,
        policy: QuotaPolicy,
        timer: CapacityTimer,
    ) -> None:
        """Bind the gate to the run's control channel, event sink, policy and timer."""
        self._control = control
        self._events = events
        self._policy = policy
        self._timer = timer
        self._waited = 0.0

    def wait_for_capacity(
        self, error: AgentQuotaError, turn: AgentTurnRequest, *, role: str, attempt: int
    ) -> None:
        """Pause the run, publish why, and return once the turn may be sent again."""
        context = {
            "agent_kind": role,
            "round_label": turn.label,
            "invocation_id": turn.invocation_id,
        }
        if attempt == 1:
            self._waited = 0.0
        now = self._timer.now()
        decision = decide_quota(
            self._policy, resets_at=error.resets_at, now=now, waited=self._waited
        )
        if isinstance(decision, GiveUp):
            self._events.quota_abandoned(error, reason=decision.reason, **context)
            raise error
        resumes_at = now + decision.seconds if isinstance(decision, WaitFor) else None
        self._events.quota_paused(error, resumes_at=resumes_at, **context)
        self._control.request_pause()
        if isinstance(decision, WaitFor):
            by_operator = self._timer.wait_resumed(decision.seconds)
            self._waited += self._timer.now() - now
        else:
            self._control.wait_while_paused()
            by_operator = True
        self._events.quota_resumed(
            error.provider, reason="operator" if by_operator else "wait_elapsed", **context
        )
