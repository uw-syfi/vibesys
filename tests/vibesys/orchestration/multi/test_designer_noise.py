"""The designer sets its bar knowing the measurement noise of the comparison before it (#3047).

An idea whose bar sits inside the noise ends inconclusive whatever it measures, so the designer
of a round is told how far a result must clear a bar for the framework to call it
(`vs_core.api.effect_resolution`), taken from the latest candidate core compared with its base. Until
a comparison states a noise the designer is told there is none, which the prompt renders as
"not yet measured". These stories play the host of `multi` over two rounds and read the context
the second designer was asked with. Every story runs live and again with a restart after every
step.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.multi._host import PLANNER, config
from tests.vibesys.serial_search._host import TERMS
from tests.vibesys.vibesys_flows import across_restarts

import vs_core.api as core
from vibesys.serial_search.api import PlanPrompt
from vs_sim.api.testing import examples

from .test_stories import design, implement, judge, start

FIRST = "def456"
SECOND = "abc789"
RATIO = 1.20
"""Far above the default bar of the plans, so an idea with this ratio is supported and the next
round designs a new hypothesis."""
NOISES = st.floats(0.001, 0.02, allow_nan=False)


def next_designer(*, restarting: bool, noises: tuple[float, ...]) -> PlanPrompt:
    """The context the designer is asked with after rounds that measured these noises."""
    comparisons = {
        commit: (RATIO, noise) for commit, noise in zip((FIRST, SECOND), noises, strict=False)
    }
    host = start(config(max_rounds=len(noises) + 1), restarting=restarting, comparisons=comparisons)
    for number, commit in enumerate((FIRST, SECOND)[: len(noises)], start=1):
        design(host, f"h{number}")
        implement(host, "implemented", commit)
        judge(host, passed=True)
        host.drain_until_turn(PLANNER)
    context = host.run.expect_turn(role=PLANNER).context
    assert isinstance(context, PlanPrompt)
    return context


@across_restarts
def test_the_designer_of_the_first_round_is_told_no_noise_is_known(*, restarting: bool) -> None:
    host = start(config(max_rounds=1), restarting=restarting)
    context = host.run.expect_turn(role=PLANNER).context
    assert isinstance(context, PlanPrompt)

    assert context.measurement_noise is None


@across_restarts
@settings(max_examples=examples(pr=6, nightly=40), deadline=None, derandomize=True, database=None)
@given(noise=NOISES)
def test_a_designer_after_a_measured_round_is_shown_that_comparisons_noise(
    *, restarting: bool, noise: float
) -> None:
    shown = next_designer(restarting=restarting, noises=(noise,)).measurement_noise

    assert shown is not None
    assert shown.noise == noise
    assert shown.resolution >= core.VERDICT_NOISES * noise
    assert (shown.candidate_samples, shown.base_samples) == (
        core.MIN_SAMPLES_WITHOUT_FLOOR,
        core.MIN_SAMPLES_WITHOUT_FLOOR,
    )


@across_restarts
@settings(max_examples=examples(pr=6, nightly=40), deadline=None, derandomize=True, database=None)
@given(first=NOISES, second=NOISES)
def test_the_latest_comparison_states_the_noise(
    *, restarting: bool, first: float, second: float
) -> None:
    shown = next_designer(restarting=restarting, noises=(first, second)).measurement_noise

    assert shown is not None
    assert shown.noise == second


@across_restarts
def test_a_comparison_that_estimates_no_noise_states_none(*, restarting: bool) -> None:
    """Runs that read alike show no noise, which is no measurement that there is none."""
    assert core.terms_policy(TERMS).floor_declared is False

    assert next_designer(restarting=restarting, noises=(0.0,)).measurement_noise is None
