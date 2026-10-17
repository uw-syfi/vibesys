"""What a run does about a provider capacity limit, as a pure decision.

The policy is configuration and the decision is arithmetic on three numbers (the
provider's reset time, the current time, and how long this turn has already
waited), so the gate that applies it holds no logic of its own and the decision
is testable over all inputs without a clock.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vs_runtime._fallback import FallbackTarget

#: Seconds added to a provider's reset time before the turn is sent again, so a
#: request does not race the provider's own clock for the reopened window.
RESET_MARGIN_SECONDS = 5.0

#: How long to wait between attempts when the provider gave no reset time.
DEFAULT_RETRY_SECONDS = 300.0


class QuotaAction(StrEnum):
    """What the run does when a turn stops on a provider capacity limit."""

    PAUSE = "pause"
    """Pause the run and wait for the operator to resume it."""
    WAIT = "wait"
    """Pause the run, then resume it by itself when capacity should have returned."""
    FAIL = "fail"
    """Do not pause: the turn fails with the quota error."""
    FALLBACK = "fallback"
    """Wait as ``WAIT`` does, then switch to the fallback provider instead of failing."""


@dataclass(frozen=True, slots=True)
class QuotaPolicy:
    """The unattended behavior for a capacity limit.

    ``wait_seconds`` bounds the total time one turn may spend waiting and applies
    to ``WAIT`` (``None`` waits as long as it takes) and ``FALLBACK`` (``None``
    does not wait: the switch is immediate). ``retry_seconds`` is the interval
    between attempts when the provider reported no reset time. ``fallback`` is
    where ``FALLBACK`` switches to and where an operator may send a paused run;
    any action may carry one, and ``FALLBACK`` requires it.
    """

    action: QuotaAction = QuotaAction.PAUSE
    wait_seconds: float | None = None
    retry_seconds: float = DEFAULT_RETRY_SECONDS
    fallback: FallbackTarget | None = None

    def __post_init__(self) -> None:
        """Reject values no decision can use."""
        if not math.isfinite(self.retry_seconds) or self.retry_seconds <= 0:
            message = f"retry_seconds must be a positive number, got {self.retry_seconds!r}"
            raise ValueError(message)
        if self.action is QuotaAction.FALLBACK and self.fallback is None:
            message = "the fallback action needs a fallback provider and model"
            raise ValueError(message)
        if self.wait_seconds is not None:
            if self.action not in (QuotaAction.WAIT, QuotaAction.FALLBACK):
                message = (
                    f"wait_seconds applies only to the wait and fallback actions, "
                    f"not {self.action.value}"
                )
                raise ValueError(message)
            if not math.isfinite(self.wait_seconds) or self.wait_seconds <= 0:
                message = f"wait_seconds must be a positive number, got {self.wait_seconds!r}"
                raise ValueError(message)


PAUSE_FOR_OPERATOR = QuotaPolicy()


@dataclass(frozen=True, slots=True)
class Hold:
    """Park until the operator resumes the run."""


@dataclass(frozen=True, slots=True)
class WaitFor:
    """Park for this many seconds, then resume the run and send the turn again."""

    seconds: float


@dataclass(frozen=True, slots=True)
class GiveUp:
    """Do not wait: end the turn with the quota error."""

    reason: str


@dataclass(frozen=True, slots=True)
class Switch:
    """Do not wait any longer: switch to the fallback provider."""

    reason: str


type QuotaDecision = Hold | WaitFor | GiveUp | Switch


@dataclass(frozen=True, slots=True)
class OutagePolicy:
    """How long one turn waits out a provider outage, and how often it tries again.

    An outage (``QuotaCondition.UNAVAILABLE``) names no limit and no reset time: the
    provider kept failing with transient errors. Nobody can decide anything about it,
    so unlike a quota it never waits for the operator. The turn is sent again after
    ``first_wait_seconds``, then after waits that double up to ``max_wait_seconds``,
    until ``budget_seconds`` have passed since the turn's first stop; then the turn
    fails with the error, as it did before there was a gate.
    """

    budget_seconds: float = 6 * 3600.0
    """Six hours. It is the longest one turn may already run (vs_agent's
    ``DEFAULT_TURN_TIMEOUT_S``), so waiting it out holds a turn no longer than working
    does. It covers the gateway outages seen on live runs (503 "No available accounts"
    for hours on 2026-10-16) and an incident at a provider, which is usually resolved
    within hours. An outage longer than that is more likely a revoked account or a
    removed model, which waiting cannot fix, and the run should end and say so. A stop
    or the run's deadline ends the wait sooner."""
    first_wait_seconds: float = 60.0
    max_wait_seconds: float = 600.0
    """Ten minutes. Each attempt also spends agentshim's own retries (about fifteen
    minutes, ``TRANSIENT_RETRY_DELAYS_S``) before it stops again, so a stalled turn
    probes the provider about every 25 minutes at most."""

    def __post_init__(self) -> None:
        """Reject values no decision can use."""
        values = (self.budget_seconds, self.first_wait_seconds, self.max_wait_seconds)
        if not all(math.isfinite(value) and value > 0 for value in values):
            message = f"outage policy seconds must be positive numbers, got {values!r}"
            raise ValueError(message)
        if self.first_wait_seconds > self.max_wait_seconds:
            message = "first_wait_seconds must not exceed max_wait_seconds"
            raise ValueError(message)


PROVIDER_OUTAGE = OutagePolicy()


def decide_outage(
    policy: OutagePolicy, *, stop: int, elapsed: float
) -> WaitFor | GiveUp:
    """Decide what to do about the ``stop``-th outage stop of one turn (the first is 1).

    ``elapsed`` is the time since the turn's first stop. A wait never runs past the
    budget, so the time from the first stop to the last attempt is at most the budget.
    """
    remaining = policy.budget_seconds - elapsed
    if remaining <= 0:
        return GiveUp(
            f"the provider stayed unavailable for {elapsed:g}s, "
            f"the whole {policy.budget_seconds:g}s outage budget"
        )
    doublings = min(max(stop - 1, 0), 32)  # 2**32 minutes is past any cap
    wanted = min(policy.first_wait_seconds * 2.0**doublings, policy.max_wait_seconds)
    return WaitFor(min(wanted, remaining))


def capacity_returns_at(*, resets_at: float | None, now: float) -> float | None:
    """When capacity should be back: the provider's reset time plus the margin, if still ahead."""
    if resets_at is not None and resets_at + RESET_MARGIN_SECONDS > now:
        return resets_at + RESET_MARGIN_SECONDS
    return None


def capacity_wait(policy: QuotaPolicy, *, resets_at: float | None, now: float) -> float:
    """How long to wait before sending the turn again: until capacity returns, else one retry."""
    back = capacity_returns_at(resets_at=resets_at, now=now)
    return policy.retry_seconds if back is None else back - now


def decide_quota(
    policy: QuotaPolicy, *, resets_at: float | None, now: float, waited: float
) -> QuotaDecision:
    """Decide what to do about one capacity stop.

    ``waited`` is the time this turn has already spent waiting on earlier stops.
    A wait never takes the total past ``wait_seconds``, and a reset time that
    falls beyond the remaining budget gives up at once instead of waiting for
    nothing.
    """
    if policy.action is QuotaAction.PAUSE:
        return Hold()
    if policy.action is QuotaAction.FAIL:
        return GiveUp("the quota policy is fail")
    budget = policy.wait_seconds
    if policy.action is QuotaAction.FALLBACK and budget is None:
        budget = 0.0
    remaining = None if budget is None else budget - waited
    end = Switch if policy.action is QuotaAction.FALLBACK else GiveUp
    if remaining is not None and remaining <= 0:
        return end(f"waited {waited:g}s for capacity, the whole budget")
    wanted = capacity_wait(policy, resets_at=resets_at, now=now)
    if capacity_returns_at(resets_at=resets_at, now=now) is not None:
        if remaining is not None and wanted > remaining:
            return end(f"capacity returns in {wanted:g}s, past the {remaining:g}s still allowed")
        return WaitFor(wanted)
    return WaitFor(wanted if remaining is None else min(wanted, remaining))
