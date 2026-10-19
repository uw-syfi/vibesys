"""The quota and outage decisions are arithmetic over a policy and the turn's times so far."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_runtime.api.infrastructure import (
    PROVIDER_OUTAGE,
    RESET_MARGIN_SECONDS,
    FallbackTarget,
    GiveUp,
    Hold,
    OutagePolicy,
    PauseFor,
    QuotaAction,
    QuotaPolicy,
    Switch,
    WaitFor,
    decide_outage,
    decide_quota,
    describe_seconds,
    outage_policy_for,
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
    lambda budget, first, extra, switch: OutagePolicy(budget, first, first + extra, switch),
    budget=seconds,
    first=st.floats(min_value=0.001, max_value=1e4, allow_nan=False),
    extra=st.floats(min_value=0, max_value=1e4, allow_nan=False),
    switch=st.booleans(),
)


@given(policy=outage_policies, stop=st.integers(min_value=1, max_value=200), elapsed=seconds)
def test_an_outage_waits_within_the_cap_and_the_budget_or_gives_up_once_it_is_spent(
    policy: OutagePolicy, stop: int, elapsed: float
) -> None:
    decision = decide_outage(policy, stop=stop, elapsed=elapsed, run_left=None)

    if elapsed >= policy.budget_seconds:
        assert isinstance(decision, Switch if policy.switch_when_spent else GiveUp)
        return
    assert isinstance(decision, WaitFor)
    assert 0 < decision.seconds <= policy.max_wait_seconds
    assert elapsed + decision.seconds <= policy.budget_seconds


@given(policy=outage_policies, stop=st.integers(min_value=1, max_value=200))
def test_outage_waits_never_shrink_from_one_stop_to_the_next_while_budget_remains(
    policy: OutagePolicy, stop: int
) -> None:
    now = decide_outage(policy, stop=stop, elapsed=0.0, run_left=None)
    later = decide_outage(policy, stop=stop + 1, elapsed=0.0, run_left=None)

    assert isinstance(now, WaitFor)
    assert isinstance(later, WaitFor)
    assert now.seconds <= later.seconds
    assert decide_outage(policy, stop=1, elapsed=0.0, run_left=None) == WaitFor(
        min(policy.first_wait_seconds, policy.budget_seconds)
    )


@pytest.mark.parametrize(
    ("budget", "first", "cap"),
    [(0, 1, 1), (1, 0, 1), (1, 2, 1), (float("inf"), 1, 1), (1, 1, float("nan"))],
)
def test_an_outage_policy_rejects_values_no_decision_can_use(
    budget: float, first: float, cap: float
) -> None:
    with pytest.raises(ValueError, match=r"outage policy|first_wait_seconds"):
        OutagePolicy(budget, first, cap)


@given(
    policy=outage_policies,
    stop=st.integers(min_value=1, max_value=200),
    elapsed=seconds,
    run_left=st.floats(min_value=-1e6, max_value=1e6, allow_nan=False),
)
def test_an_outage_wait_never_runs_past_the_run_deadline(
    policy: OutagePolicy, stop: int, elapsed: float, run_left: float
) -> None:
    """The run's deadline bounds every wait, and a run out of time does not wait at all."""
    decision = decide_outage(policy, stop=stop, elapsed=elapsed, run_left=run_left)

    if run_left <= 0:
        assert isinstance(decision, GiveUp)
        assert "deadline" in decision.reason
        return
    if isinstance(decision, WaitFor):
        assert 0 < decision.seconds <= run_left
    else:
        assert elapsed >= policy.budget_seconds


def test_an_outage_that_spent_its_budget_says_so_in_hours_and_minutes() -> None:
    decision = decide_outage(PROVIDER_OUTAGE, stop=40, elapsed=6 * 3600 + 125.0, run_left=None)

    assert decision == GiveUp(
        "the provider stayed unavailable for 6 h 2 min 5 s, the whole 6 h outage budget"
    )


_TARGET = FallbackTarget("codex", "gpt-6-luna")


def _quota_policy(action: QuotaAction) -> QuotaPolicy:
    return QuotaPolicy(action, fallback=_TARGET if action is QuotaAction.FALLBACK else None)


@given(
    action=st.sampled_from(QuotaAction),
    stops=st.lists(seconds, min_size=1, max_size=40),
)
def test_the_quota_policy_bounds_how_long_an_outage_is_waited_out(
    action: QuotaAction, stops: list[float]
) -> None:
    """``pause`` and ``wait`` wait an outage out for the whole budget, then pause the run (S179):
    ``pause`` for the operator, ``wait`` until it resumes by itself; ``fail`` and ``fallback``
    wait one backoff step, then fail or switch to the fallback.

    ``stops`` are the times each later stop took to come back (the send's own retries); the
    turn waits what each decision says, and the sequence ends at the first decision that
    does not wait.
    """
    policy = outage_policy_for(_quota_policy(action))
    elapsed = 0.0
    waits: list[float] = []
    decision = decide_outage(policy, stop=1, elapsed=0.0, run_left=None)
    for stop, send in enumerate(stops, start=2):
        if not isinstance(decision, WaitFor):
            break
        waits.append(decision.seconds)
        elapsed += decision.seconds + send
        decision = decide_outage(policy, stop=stop, elapsed=elapsed, run_left=None)

    first = PROVIDER_OUTAGE.first_wait_seconds
    if action is QuotaAction.PAUSE:
        assert policy == replace(PROVIDER_OUTAGE, pause_when_spent=True)
        assert isinstance(decision, WaitFor | Hold)
    elif action is QuotaAction.WAIT:
        # ``wait`` resumes by itself once the budget is spent, one retry interval later.
        retry = _quota_policy(action).retry_seconds
        assert policy == replace(PROVIDER_OUTAGE, pause_when_spent=True, resume_after_seconds=retry)
        assert decision == PauseFor(retry) or isinstance(decision, WaitFor)
    else:
        # One backoff step, then the policy's own end: a switch only under fallback.
        assert waits == [first]
        assert isinstance(decision, Switch if action is QuotaAction.FALLBACK else GiveUp)
        assert describe_seconds(first) in decision.reason


@given(left=seconds)
def test_a_deadline_never_switches_providers(left: float) -> None:
    """A run out of time ends the turn; switching would only start work that cannot finish."""
    policy = outage_policy_for(_quota_policy(QuotaAction.FALLBACK))

    decision = decide_outage(policy, stop=2, elapsed=policy.budget_seconds + left, run_left=-left)

    assert isinstance(decision, GiveUp)


@given(waited=seconds, wanted=seconds)
def test_quota_give_up_reasons_state_durations_in_hours_minutes_and_seconds(
    waited: float, wanted: float
) -> None:
    spent = decide_quota(
        QuotaPolicy(QuotaAction.WAIT, wait_seconds=waited), resets_at=None, now=1e9, waited=waited
    )
    now, resets_at = 1e9, 1e9 + 2e8 + wanted
    late = decide_quota(
        QuotaPolicy(QuotaAction.WAIT, wait_seconds=1e8), resets_at=resets_at, now=now, waited=0.0
    )

    assert spent == GiveUp(f"waited {describe_seconds(waited)} for capacity, the whole budget")
    returns_in = describe_seconds(resets_at + RESET_MARGIN_SECONDS - now)
    assert late == GiveUp(
        f"capacity returns in {returns_in}, past the {describe_seconds(1e8)} still allowed"
    )


@pytest.mark.regression
@example(spent=3600.0, retry=300.0)
@given(spent=seconds, retry=st.floats(min_value=1.0, max_value=86_400.0))
def test_a_spent_outage_budget_resumes_by_itself_only_under_wait(
    spent: float, retry: float
) -> None:
    """Past the budget ``wait`` pauses on a timed resume, as documented on ``QuotaAction.WAIT``;
    ``pause`` waits for the operator. Before (review of #3283, MED-3), both waited for the
    operator."""
    elapsed = PROVIDER_OUTAGE.budget_seconds + spent
    wait = outage_policy_for(QuotaPolicy(QuotaAction.WAIT, retry_seconds=retry))
    pause = outage_policy_for(QuotaPolicy(QuotaAction.PAUSE, retry_seconds=retry))

    assert decide_outage(wait, stop=9, elapsed=elapsed, run_left=None) == PauseFor(retry)
    assert decide_outage(pause, stop=9, elapsed=elapsed, run_left=None) == Hold()
