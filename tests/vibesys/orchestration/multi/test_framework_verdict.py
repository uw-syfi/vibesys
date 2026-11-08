"""The framework, not the implementer, decides whether an idea held (#3047).

An implementer reports implementation status only. Once the judge passes a candidate, the
framework measures it, compares it with its base (core's comparison over every sample of its
measured input, the agent's own evaluations included) and derives supported, within noise or
disproven from the plan's typed bar. These stories play the host of `multi` and read what the
search recorded. Every story runs live and again with a restart after every step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.architecture.decision_input_cases import cases, renderer
from tests.support.prompt_context import registered_searches
from tests.vibesys.orchestration.multi._host import JUDGE, PLANNER, config, reviewed
from tests.vibesys.serial_search._host import TERMS
from tests.vibesys.serial_search._replies import plan_reply
from tests.vibesys.vibesys_flows import across_restarts

import vs_core.api as core
from vibesys.hypothesis import ImplementerStatus, MechanismVerdict, RoundRecord
from vibesys.orchestration.multi.models import ImplementerResponse
from vibesys.serial_search.api import PlanPrompt, RoundRow
from vs_core.api import RevisionRef
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from pathlib import Path

from .test_stories import design, implement, judge, start

CANDIDATE = "def456"
OUTCOME = {
    core.EffectVerdict.SUPPORTED: "proven",
    core.EffectVerdict.WITHIN_NOISE: "inconclusive",
    core.EffectVerdict.DISPROVEN: "disproven",
}


GAIN = {
    core.EffectVerdict.SUPPORTED: "better",
    core.EffectVerdict.WITHIN_NOISE: "within_noise",
    core.EffectVerdict.DISPROVEN: "worse",
}
_ORDER = ("worse", "within_noise", "better")


def played_round(
    *, restarting: bool, bar: float, ratio: float, noise: float, **review: object
) -> RoundRecord:
    """One round of an idea with this bar, measured at this ratio over its base: its record.

    ``review`` adds fields to the judge's passing reply.
    """
    host = start(
        config(max_rounds=1), restarting=restarting, comparisons={CANDIDATE: (ratio, noise)}
    )
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply("h1", minimum_ratio=bar))
    implement(host, "implemented", CANDIDATE)
    host.drain_until_turn(JUDGE)
    host.run.reply(host.run.expect_turn(role=JUDGE), reviewed(passed=True, **review))
    host.drain()
    record = host.record
    assert record is not None
    (round_record,) = record.hypotheses[0].rounds
    assert CANDIDATE in host.measured
    return round_record


def played(*, restarting: bool, bar: float, ratio: float, noise: float) -> str:
    """One round of an idea with this bar, measured at this ratio over its base: its outcome."""
    outcome = played_round(
        restarting=restarting, bar=bar, ratio=ratio, noise=noise
    ).hypothesis_outcome
    assert outcome is not None
    return outcome


def comparison_at(ratio: float, noise: float) -> core.Comparison:
    """The comparison the host states for the candidate at this ratio and noise."""
    return core.Comparison(
        scope=core.Scope(owner=core.AttemptId(root="a"), generation=0),
        metric="throughput",
        direction="max",
        candidate=RevisionRef.of_git_commit(CANDIDATE),
        candidate_input="x",
        base=RevisionRef.of_git_commit("base"),
        base_input="y",
        candidate_value=ratio,
        base_value=1.0,
        ratio=ratio,
        noise=noise,
        verdict=core.ComparisonVerdict.WITHIN_NOISE,
        candidate_samples=core.MIN_SAMPLES_WITHOUT_FLOOR,
        base_samples=core.MIN_SAMPLES_WITHOUT_FLOOR,
    )


@across_restarts
def test_an_idea_that_clears_its_bar_beyond_noise_is_supported_without_anyone_saying_so(
    *, restarting: bool
) -> None:
    assert played(restarting=restarting, bar=1.05, ratio=1.20, noise=0.02) == "proven"


@across_restarts
def test_an_idea_inside_the_noise_of_its_bar_is_inconclusive_even_when_it_beats_the_base(
    *, restarting: bool
) -> None:
    """+6% against a 5% bar with 2% noise is a close call, not a win: the old comparison said win."""
    assert played(restarting=restarting, bar=1.05, ratio=1.06, noise=0.02) == "inconclusive"


@across_restarts
def test_an_idea_that_misses_its_bar_beyond_noise_is_disproven(*, restarting: bool) -> None:
    assert played(restarting=restarting, bar=1.05, ratio=1.00, noise=0.01) == "disproven"


@across_restarts
@settings(max_examples=examples(pr=12, nightly=60), deadline=None, derandomize=True, database=None)
@given(
    bar=st.floats(1.0, 1.5, allow_nan=False),
    ratio=st.floats(0.6, 1.9, allow_nan=False),
    noise=st.floats(0.001, 0.08, allow_nan=False),
)
def test_the_recorded_outcome_is_what_the_pure_verdict_derives_from_the_comparison(
    *, restarting: bool, bar: float, ratio: float, noise: float
) -> None:
    policy = core.terms_policy(TERMS)
    expected = OUTCOME[core.effect_verdict(comparison_at(ratio, noise), bar, policy)]
    assert played(restarting=restarting, bar=bar, ratio=ratio, noise=noise) == expected


@pytest.mark.regression
@across_restarts
def test_a_real_gain_that_misses_its_bar_is_recorded_as_a_gain_not_only_as_disproven(
    *, restarting: bool
) -> None:
    """A candidate 4% better than its base, against a 10% forecast, is a real gain.

    In the 2026-10-09 multimodal campaign such gains (Waypoint round 7, pi0.5 round 16) were
    recorded only as a missed bar, and the next designer read the direction as falsified. The
    bar's verdict stays the plan's; the record also states that the candidate beat its base.
    """
    round_record = played_round(restarting=restarting, bar=1.10, ratio=1.04, noise=0.005)
    assert round_record.hypothesis_outcome == "disproven"
    assert round_record.perf_gain == "better"


@across_restarts
@settings(max_examples=examples(pr=12, nightly=60), deadline=None, derandomize=True, database=None)
@given(
    bar=st.floats(1.0, 1.5, allow_nan=False),
    ratio=st.floats(0.6, 1.9, allow_nan=False),
    noise=st.floats(0.001, 0.08, allow_nan=False),
)
def test_the_recorded_gain_asks_for_any_gain_and_is_never_below_the_bars_verdict(
    *, restarting: bool, bar: float, ratio: float, noise: float
) -> None:
    """The gain is the same comparison held to a bar of one; a harder bar can only lower it."""
    policy = core.terms_policy(TERMS)
    expected = GAIN[core.effect_verdict(comparison_at(ratio, noise), 1.0, policy)]
    round_record = played_round(restarting=restarting, bar=bar, ratio=ratio, noise=noise)
    assert round_record.perf_gain == expected
    assert round_record.perf_comparison is not None
    assert _ORDER.index(round_record.perf_gain) >= _ORDER.index(round_record.perf_comparison)


def test_an_implementer_can_report_implementation_status_and_nothing_else() -> None:
    """supported, disproven and inconclusive are the framework's words, never a reply's."""
    allowed = {status.value for status in ImplementerStatus}
    assert allowed == {"implemented", "continue", "implementation_failed", "blocked"}
    schema = ImplementerResponse.model_json_schema()
    assert set(schema["$defs"]["ImplementerStatus"]["enum"]) == allowed
    for word in ("supported", "disproven", "inconclusive", "nominated"):
        assert word not in allowed


def test_an_idea_the_framework_has_no_comparison_for_is_inconclusive_never_supported() -> None:
    """With no base's samples there is nothing to derive a verdict from, and nobody's word counts.

    This is the one case left that the live run showed as inconclusive "for lack of a base": the
    comparison exists whenever the task has a benchmark stage, because core measures the base.
    """
    host = start(config(max_rounds=1), restarting=False)
    design(host)
    implement(host, "implemented", CANDIDATE)
    judge(host, passed=True)
    host.drain()
    record = host.record
    assert record is not None
    (round_record,) = record.hypotheses[0].rounds
    assert round_record.hypothesis_outcome == "inconclusive"


@pytest.mark.regression
@across_restarts
def test_a_gain_from_another_mechanism_is_recorded_as_a_gain_with_its_mechanism_refuted(
    *, restarting: bool
) -> None:
    """The measured gain and the judge's verdict on the plan's mechanism are recorded apart.

    In the 2026-10-09 multimodal campaign pi0.5 round 2 was 1.9% faster, but from vectorized
    epilogues rather than the barrier removal the plan proposed. One outcome word could not say
    both, and the next designer abandoned synchronization work that a later round showed paid.
    """
    attribution = "vectorized epilogues; the removed barriers saved nothing"
    round_record = played_round(
        restarting=restarting,
        bar=1.0,
        ratio=1.2,
        noise=0.01,
        mechanism="refuted",
        mechanism_attribution=attribution,
    )
    assert round_record.hypothesis_outcome == "proven"
    assert round_record.perf_gain == "better"
    assert round_record.mechanism_verdict is MechanismVerdict.REFUTED
    assert round_record.mechanism_attribution == attribution


@across_restarts
@settings(max_examples=examples(pr=12, nightly=60), deadline=None, derandomize=True, database=None)
@given(
    mechanism=st.sampled_from(MechanismVerdict),
    attribution=st.text(st.characters(codec="utf-8", exclude_categories=("Cs",)), max_size=40),
    ratio=st.floats(0.6, 1.9, allow_nan=False),
)
def test_the_judges_mechanism_verdict_is_recorded_and_never_changes_the_measured_outcome(
    *, restarting: bool, mechanism: MechanismVerdict, attribution: str, ratio: float
) -> None:
    """Whatever the judge says of the mechanism, the outcome and gain stay the framework's."""
    plain = played_round(restarting=restarting, bar=1.05, ratio=ratio, noise=0.02)
    stated = played_round(
        restarting=restarting,
        bar=1.05,
        ratio=ratio,
        noise=0.02,
        mechanism=mechanism.value,
        mechanism_attribution=attribution,
    )
    assert stated.mechanism_verdict is mechanism
    assert stated.mechanism_attribution == (attribution or None)
    assert (stated.hypothesis_outcome, stated.perf_gain, stated.perf_comparison) == (
        plain.hypothesis_outcome,
        plain.perf_gain,
        plain.perf_comparison,
    )


def test_a_judge_that_states_no_mechanism_records_it_as_untested() -> None:
    """An older judge reply without the field still validates, and claims nothing."""
    round_record = played_round(restarting=False, bar=1.0, ratio=1.2, noise=0.01)
    assert round_record.mechanism_verdict is MechanismVerdict.UNTESTED
    assert round_record.mechanism_attribution is None


@pytest.mark.regression
@across_restarts
def test_the_next_designer_sees_the_gain_and_the_mechanism_of_a_round_apart(
    *, restarting: bool
) -> None:
    """The history row the next designer reads carries all three verdicts of the round."""
    attribution = "the new barrier primitive, not fewer barriers"
    host = start(
        config(max_rounds=2), restarting=restarting, comparisons={CANDIDATE: (1.04, 0.005)}
    )
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply("h1", minimum_ratio=1.10))
    implement(host, "implemented", CANDIDATE)
    host.drain_until_turn(JUDGE)
    host.run.reply(
        host.run.expect_turn(role=JUDGE),
        reviewed(passed=True, mechanism="refuted", mechanism_attribution=attribution),
    )
    host.drain_until_turn(PLANNER)
    context = host.run.expect_turn(role=PLANNER).context
    assert isinstance(context, PlanPrompt)

    (row,) = context.history
    assert (row.outcome, row.gain, row.mechanism) == ("disproven", "better", "refuted")
    assert row.attribution == attribution


def test_the_designers_history_states_a_gain_below_the_bar_and_a_refuted_mechanism(
    tmp_path: Path,
) -> None:
    """The rendered row says the round beat its parent and what the judge credited."""
    case = cases()["multi designer"]
    search = next(item for item in registered_searches() if item.name == case.search)
    row = RoundRow(
        round_number=4,
        hypothesis_id="h4",
        passed=True,
        official=True,
        judge_verdict="pass",
        outcome="disproven",
        metric=8.34,
        unit="kernel_latency_ms",
        delta_pct=-0.7,
        gain="better",
        mechanism="refuted",
        attribution="attributed-edit-z",
    )
    plain = row.model_copy(update={"gain": None, "mechanism": None, "attribution": ""})

    def rendered(history: RoundRow) -> str:
        context = case.context.model_copy(update={"history": (history,)})
        root = tmp_path / history.hypothesis_id / str(history.mechanism)
        root.mkdir(parents=True)
        return renderer(search).render(root, context, subject=case.role, held=case.held)

    text = rendered(row)
    assert "outcome disproven, official 8.34 kernel_latency_ms (-0.7% vs its parent)" in text
    assert ", beat its parent, mechanism refuted (judge: attributed-edit-z)" in text
    without = rendered(plain)
    assert "mechanism refuted" not in without
    assert "beat its parent," not in without
