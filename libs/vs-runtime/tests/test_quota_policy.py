"""The quota decision is arithmetic over (policy, reset time, now, time already waited)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_runtime.api.infrastructure import (
    RESET_MARGIN_SECONDS,
    GiveUp,
    Hold,
    QuotaAction,
    QuotaPolicy,
    WaitFor,
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
