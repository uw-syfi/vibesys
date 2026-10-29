"""What a run does about a provider capacity limit, as a pure decision.

The policy is configuration and the decision is arithmetic on three numbers (the
provider's reset time, the current time, and how long this turn has already
waited), so the gate that applies it holds no logic of its own and the decision
is testable over all inputs without a clock.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
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
class PauseFor:
    """Pause the run, and resume it by itself this many seconds later (or when the operator does).

    An outage that outlasted its budget under ``wait``: the run waits for capacity on its own,
    as ``wait`` promises, probing the provider once per resume.
    """

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
    provider kept failing with transient errors. Within its budget nobody needs to decide
    anything about it, so unlike a quota it holds only the turn, not the run. The turn is
    sent again after ``first_wait_seconds``, then after waits that double up to
    ``max_wait_seconds``, until ``budget_seconds`` have passed since the turn's first stop.
    Then, with ``pause_when_spent``, the run pauses, as for a quota: for the operator, or with
    ``resume_after_seconds`` until it resumes by itself that much later; with
    ``switch_when_spent`` the run switches to its fallback provider; with neither the turn
    fails with the error. :func:`outage_policy_for` derives the policy a run's quota
    policy asks for.
    """

    budget_seconds: float = 6 * 3600.0
    """Six hours. It is the longest one turn may already run (vs_agent's
    ``DEFAULT_TURN_TIMEOUT_S``), so waiting it out holds a turn no longer than working
    does. It covers the gateway outages seen on live runs (503 "No available accounts"
    for hours on 2026-10-16) and an incident at a provider, which is usually resolved
    within hours. An outage longer than that is more likely a revoked account or a
    removed model, which waiting cannot fix, and the run should end and say so.

    The budget is per turn and bounds only the waiting. Sooner ends come from outside
    the policy: a stop request or a cancellation of the turn ends a wait at once, and
    no wait runs past the run's deadline (``decide_outage`` takes the time the run has
    left), so an outage never holds a run beyond its deadline."""
    first_wait_seconds: float = 60.0
    max_wait_seconds: float = 600.0
    """Ten minutes. Each attempt also spends the provider CLI's own retries before it stops
    again (agentshim retries none, ``TRANSIENT_RETRY_DELAYS_S``), so a stalled turn probes
    the provider about every quarter hour at most."""
    switch_when_spent: bool = False
    """Switch to the fallback provider once the budget is spent, instead of failing."""
    pause_when_spent: bool = False
    """Pause the run for the operator once the budget is spent, instead of failing (S179:
    an outage must never end a run that asked to wait). The turn stays held, its limit
    uncharged, until the operator resumes the run; then it is sent again and a new budget
    starts. ``switch_when_spent`` takes precedence."""
    resume_after_seconds: float | None = None
    """With ``pause_when_spent``, the run's pause resumes by itself after this many seconds
    (``wait``'s ``retry_seconds``), and the turn is sent again; None waits for the operator
    (``pause``)."""
    resend_grace_seconds: float = 600.0
    """Ten minutes: the least time the send after a capacity wait has before core may end the
    turn, whatever own time the turn had left (core's ``CapacityHold.resend_grace``). A failing
    send took 3 to 4 min per CLI attempt on S179 (the CLI's own retries), so a turn
    whose outage started late in its own time is not cut while its resend still fails; the
    next stop reopens the hold first. The gate raises it to twice the longest failing send it
    saw of the turn. It is also the most a resend that succeeds runs past the turn's limit."""

    def __post_init__(self) -> None:
        """Reject values no decision can use."""
        values = (
            self.budget_seconds,
            self.first_wait_seconds,
            self.max_wait_seconds,
            self.resend_grace_seconds,
        )
        if not all(math.isfinite(value) and value > 0 for value in values):
            message = f"outage policy seconds must be positive numbers, got {values!r}"
            raise ValueError(message)
        if self.first_wait_seconds > self.max_wait_seconds:
            message = "first_wait_seconds must not exceed max_wait_seconds"
            raise ValueError(message)
        resume = self.resume_after_seconds
        if resume is not None and not (math.isfinite(resume) and resume > 0):
            message = f"resume_after_seconds must be a positive number, got {resume!r}"
            raise ValueError(message)


PROVIDER_OUTAGE = OutagePolicy()


def outage_policy_for(quota: QuotaPolicy, base: OutagePolicy = PROVIDER_OUTAGE) -> OutagePolicy:
    """The outage policy a run with quota policy ``quota`` follows.

    ``pause`` and ``wait`` wait an outage out for the whole budget, then pause the run, so an
    outage never ends a run that chose to wait for capacity: ``pause`` until the operator
    resumes it, ``wait`` until it resumes by itself ``retry_seconds`` later (the interval
    between attempts when the provider names no reset time, as an outage never does), as
    ``wait`` does for a quota. ``fail`` and
    ``fallback`` say the run should not sit on a provider that cannot serve it, so
    they allow one backoff step (``first_wait_seconds``, enough for a blip to pass);
    then ``fail`` fails the turn and ``fallback`` switches to the fallback provider.
    """
    if quota.action is QuotaAction.PAUSE:
        return replace(base, pause_when_spent=True)
    if quota.action is QuotaAction.WAIT:
        return replace(base, pause_when_spent=True, resume_after_seconds=quota.retry_seconds)
    return replace(
        base,
        budget_seconds=base.first_wait_seconds,
        switch_when_spent=quota.action is QuotaAction.FALLBACK,
    )


def decide_outage(
    policy: OutagePolicy, *, stop: int, elapsed: float, run_left: float | None
) -> Hold | PauseFor | WaitFor | GiveUp | Switch:
    """Decide what to do about the ``stop``-th outage stop of one turn (the first is 1).

    ``elapsed`` is the time since the turn's first outage stop and ``run_left`` the time
    to the run's deadline (``None`` for a run without one). A wait never runs past the
    budget nor past the deadline, so the time from the first stop to the last attempt is
    at most the budget, and a turn never waits once the run is out of time. A spent
    budget switches providers under ``switch_when_spent``, else pauses the run under
    ``pause_when_spent``: for ``resume_after_seconds`` when set, else for the operator; a
    deadline does neither.
    """
    if run_left is not None and run_left <= 0:
        return GiveUp("the run reached its deadline while the provider was unavailable")
    remaining = policy.budget_seconds - elapsed
    if remaining <= 0:
        if not policy.switch_when_spent and policy.pause_when_spent:
            resume = policy.resume_after_seconds
            return Hold() if resume is None else PauseFor(resume)
        end = Switch if policy.switch_when_spent else GiveUp
        return end(
            f"the provider stayed unavailable for {describe_seconds(elapsed)}, "
            f"the whole {describe_seconds(policy.budget_seconds)} outage budget"
        )
    doublings = min(max(stop - 1, 0), 32)  # 2**32 minutes is past any cap
    wanted = min(policy.first_wait_seconds * 2.0**doublings, policy.max_wait_seconds)
    bound = remaining if run_left is None else min(remaining, run_left)
    return WaitFor(min(wanted, bound))


def describe_seconds(seconds: float) -> str:
    """A duration in the largest units that keep it exact to the second: ``2 h 5 min``."""
    whole = max(round(seconds), 0)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [
        f"{value} {unit}" for value, unit in ((hours, "h"), (minutes, "min"), (secs, "s")) if value
    ]
    return " ".join(parts) or "0 s"


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
        return end(f"waited {describe_seconds(waited)} for capacity, the whole budget")
    wanted = capacity_wait(policy, resets_at=resets_at, now=now)
    if capacity_returns_at(resets_at=resets_at, now=now) is not None:
        if remaining is not None and wanted > remaining:
            return end(
                f"capacity returns in {describe_seconds(wanted)}, "
                f"past the {describe_seconds(remaining)} still allowed"
            )
        return WaitFor(wanted)
    return WaitFor(wanted if remaining is None else min(wanted, remaining))
