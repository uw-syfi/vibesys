"""The quota and outage decisions are arithmetic over a policy and the turn's times so far."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_runtime.api.infrastructure import (
    RESET_MARGIN_SECONDS,
    FallbackTarget,
    GiveUp,
    Hold,
    OutagePolicy,
    QuotaAction,
    QuotaPolicy,
    Switch,
    WaitFor,
    decide_outage,
    decide_quota,
)

if TYPE_CHECKING:
    from collections.abc import Callable

seconds = st.floats(min_value=0.001, max_value=1e7, allow_nan=False)
epochs = st.floats(min_value=1e9, max_value=2e9, allow_nan=False)


@given(resets_at=st.none() | epochs, now=epochs, waited=st.floats(min_value=0, max_value=1e6))
def test_pausing_always_holds_for_the_operator_and_failing_always_gives_up(
    resets_at: float | None, now: float, waited: float
) -> None:
    pause = decide_quota(QuotaPolicy(), resets_at=resets_at, now=now, waited=waited)
    fail = decide_quota(QuotaPolicy(QuotaAction.FAIL), resets_at=resets_at, now=now, waited=waited)

    assert isinstance(pause, Hold)
    assert isinstance(fail, GiveUp)


@given(
    resets_at=st.none() | epochs,
    now=epochs,
    waited=st.floats(min_value=0, max_value=1e6),
    budget=st.none() | seconds,
    retry=seconds,
)
def test_a_wait_is_positive_and_never_takes_the_total_past_the_budget(
    resets_at: float | None, now: float, waited: float, budget: float | None, retry: float
) -> None:
    policy = QuotaPolicy(QuotaAction.WAIT, wait_seconds=budget, retry_seconds=retry)

    decision = decide_quota(policy, resets_at=resets_at, now=now, waited=waited)

    if isinstance(decision, WaitFor):
        assert decision.seconds > 0
        if budget is not None:
            assert waited + decision.seconds <= budget + 1e-6
    else:
        assert isinstance(decision, GiveUp)


@given(now=epochs, ahead=seconds, retry=seconds)
def test_an_unbounded_wait_goes_to_the_reported_reset_and_then_some(
    now: float, ahead: float, retry: float
) -> None:
    policy = QuotaPolicy(QuotaAction.WAIT, retry_seconds=retry)

    decision = decide_quota(policy, resets_at=now + ahead, now=now, waited=0.0)

    assert isinstance(decision, WaitFor)
    assert decision.seconds == pytest.approx(ahead + RESET_MARGIN_SECONDS)


@given(now=epochs, behind=st.floats(min_value=RESET_MARGIN_SECONDS, max_value=1e6), retry=seconds)
def test_a_reset_already_in_the_past_falls_back_to_the_retry_interval(
    now: float, behind: float, retry: float
) -> None:
    policy = QuotaPolicy(QuotaAction.WAIT, retry_seconds=retry)

    assert decide_quota(policy, resets_at=now - behind, now=now, waited=0.0) == WaitFor(retry)


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: QuotaPolicy(retry_seconds=0), "retry_seconds"),
        (lambda: QuotaPolicy(retry_seconds=float("nan")), "retry_seconds"),
        (lambda: QuotaPolicy(QuotaAction.WAIT, wait_seconds=0), "wait_seconds"),
        (lambda: QuotaPolicy(QuotaAction.PAUSE, wait_seconds=5), "wait_seconds"),
    ],
)
def test_a_policy_no_decision_can_use_is_rejected_naming_the_field(
    build: Callable[[], QuotaPolicy], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build()


TARGET = FallbackTarget("codex", "gpt-5")


@given(resets_at=st.none() | epochs, now=epochs, waited=st.floats(min_value=0, max_value=1e6))
def test_a_fallback_with_no_wait_budget_switches_at_once(
    resets_at: float | None, now: float, waited: float
) -> None:
    policy = QuotaPolicy(QuotaAction.FALLBACK, fallback=TARGET)

    assert isinstance(decide_quota(policy, resets_at=resets_at, now=now, waited=waited), Switch)


@given(
    resets_at=st.none() | epochs,
    now=epochs,
    waited=st.floats(min_value=0, max_value=1e6),
    budget=seconds,
    retry=seconds,
)
def test_a_fallback_waits_within_its_budget_and_otherwise_switches_never_gives_up(
    resets_at: float | None, now: float, waited: float, budget: float, retry: float
) -> None:
    policy = QuotaPolicy(
        QuotaAction.FALLBACK, wait_seconds=budget, retry_seconds=retry, fallback=TARGET
    )

    decision = decide_quota(policy, resets_at=resets_at, now=now, waited=waited)

    assert isinstance(decision, (WaitFor, Switch))
    if isinstance(decision, WaitFor):
        assert 0 < decision.seconds <= budget - waited + 1e-6


@given(resets_at=st.none() | epochs, now=epochs)
def test_a_pause_policy_that_names_a_fallback_still_holds_for_the_operator(
    resets_at: float | None, now: float
) -> None:
    policy = QuotaPolicy(fallback=TARGET)

    assert isinstance(decide_quota(policy, resets_at=resets_at, now=now, waited=0.0), Hold)


def test_the_fallback_action_without_a_target_is_rejected() -> None:
    with pytest.raises(ValueError, match="fallback"):
        QuotaPolicy(QuotaAction.FALLBACK)


outage_policies = st.builds(
    lambda budget, first, extra: OutagePolicy(budget, first, first + extra),
    budget=seconds,
    first=st.floats(min_value=0.001, max_value=1e4, allow_nan=False),
    extra=st.floats(min_value=0, max_value=1e4, allow_nan=False),
)


@given(policy=outage_policies, stop=st.integers(min_value=1, max_value=200), elapsed=seconds)
def test_an_outage_waits_within_the_cap_and_the_budget_or_gives_up_once_it_is_spent(
    policy: OutagePolicy, stop: int, elapsed: float
) -> None:
    decision = decide_outage(policy, stop=stop, elapsed=elapsed)

    if elapsed >= policy.budget_seconds:
        assert isinstance(decision, GiveUp)
        return
    assert isinstance(decision, WaitFor)
    assert 0 < decision.seconds <= policy.max_wait_seconds
    assert elapsed + decision.seconds <= policy.budget_seconds


@given(policy=outage_policies, stop=st.integers(min_value=1, max_value=200))
def test_outage_waits_never_shrink_from_one_stop_to_the_next_while_budget_remains(
    policy: OutagePolicy, stop: int
) -> None:
    now = decide_outage(policy, stop=stop, elapsed=0.0)
    later = decide_outage(policy, stop=stop + 1, elapsed=0.0)

    assert isinstance(now, WaitFor)
    assert isinstance(later, WaitFor)
    assert now.seconds <= later.seconds
    assert decide_outage(policy, stop=1, elapsed=0.0) == WaitFor(
        min(policy.first_wait_seconds, policy.budget_seconds)
    )


@pytest.mark.parametrize(
    ("budget", "first", "cap"),
    [(0, 1, 1), (1, 0, 1), (1, 2, 1), (float("inf"), 1, 1), (1, 1, float("nan"))],
)
def test_an_outage_policy_rejects_values_no_decision_can_use(
    budget: float, first: float, cap: float
) -> None:
    with pytest.raises(ValueError, match="outage policy|first_wait_seconds"):
        OutagePolicy(budget, first, cap)
