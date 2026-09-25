"""Unit and property tests for ``search.profile_focus``."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vibesys.search.profile_focus import (
    ProfileAttributionError,
    ProfileBottleneck,
    ProfileFocus,
    ProfileFocusConfig,
    parse_attribution,
)

_NAMES = st.sampled_from(["attn", "mlp", "kv_cache", "norm"])
_BEGIN = "__VIBESYS_ATTRIBUTION_BEGIN__"
_END = "__VIBESYS_ATTRIBUTION_END__"


def _bottleneck_tuple(names: list[str]) -> tuple[ProfileBottleneck, ...]:
    total = sum(range(1, len(names) + 1)) or 1
    return tuple(
        ProfileBottleneck(name=name, cost=float(rank), share=rank / total, evidence=[])
        for rank, name in enumerate(reversed(names), start=1)
    )


@st.composite
def _round_of_attribution(draw):  # noqa: ANN001, ANN202
    names = draw(st.lists(_NAMES, min_size=1, max_size=4, unique=True))
    return _bottleneck_tuple(names)


def _sorted_bottleneck_tuple(names: list[str]) -> tuple[ProfileBottleneck, ...]:
    """Build attribution already in ``parse_attribution``'s cost-desc, name-tie order.

    Real attribution (from ``parse_attribution``) is always pre-sorted this
    way; both the old controller's ephemeral ranking and the persisted
    ``ranking_round`` reconstruction assume that invariant, so the reference
    model below only needs to reproduce it, not re-derive a sort order.
    """
    total = sum(range(1, len(names) + 1)) or 1
    ordered = sorted(names)
    size = len(ordered)
    return tuple(
        ProfileBottleneck(
            name=name, cost=float(size - index), share=(size - index) / total, evidence=[]
        )
        for index, name in enumerate(ordered)
    )


@st.composite
def _sorted_round_of_attribution(draw):  # noqa: ANN001, ANN202
    names = draw(st.lists(_NAMES, min_size=0, max_size=4, unique=True))
    return _sorted_bottleneck_tuple(names)


@given(
    rounds=st.lists(_round_of_attribution(), min_size=0, max_size=6),
    plateau_min_rounds=st.integers(min_value=1, max_value=3),
    min_relative_improvement=st.floats(min_value=0.0, max_value=0.2, allow_nan=False),
    improvements=st.lists(
        st.one_of(st.none(), st.floats(min_value=-0.5, max_value=0.5, allow_nan=False)),
        min_size=0,
        max_size=6,
    ),
)
def test_observe_and_record_track_one_active_component_at_a_time(
    rounds: list[tuple[ProfileBottleneck, ...]],
    plateau_min_rounds: int,
    min_relative_improvement: float,
    improvements: list[float | None],
) -> None:
    search = ProfileFocus(
        ProfileFocusConfig(
            plateau_min_rounds=plateau_min_rounds, min_relative_improvement=min_relative_improvement
        )
    )
    state = search.initial()

    for round_number, attribution in enumerate(rounds, start=1):
        state = search.observe(state, round_number=round_number, bottlenecks=attribution)
        active = state.active_component
        component_names = {component.name for component in state.components}
        assert {item.name for item in attribution} <= component_names
        if attribution and any(
            component.status.value != "exhausted" for component in state.components
        ):
            assert active is not None

        improvement = (
            improvements[round_number - 1] if round_number - 1 < len(improvements) else None
        )
        passed = improvement is not None
        state = search.record(
            state, round_number=round_number, passed=passed, relative_improvement=improvement
        )
        # ``focus`` stays a pure function of the persisted state alone.
        assert search.focus(state).active_component == (state.active_component or "")


@dataclass(frozen=True)
class _ObserveEvent:
    """One simulated ``ProfileFocus.observe`` call in an event sequence."""

    attribution: tuple[ProfileBottleneck, ...]


@dataclass(frozen=True)
class _RecordEvent:
    """One simulated ``ProfileFocus.record`` call in an event sequence."""

    passed: bool
    relative_improvement: float | None


_ProfileFocusEvent = _ObserveEvent | _RecordEvent

_events_strategy = st.lists(
    st.one_of(
        _sorted_round_of_attribution().map(_ObserveEvent),
        st.builds(
            _RecordEvent,
            passed=st.booleans(),
            relative_improvement=st.one_of(
                st.none(), st.floats(min_value=-0.5, max_value=0.5, allow_nan=False)
            ),
        ),
    ),
    min_size=0,
    max_size=12,
)


@given(
    events=_events_strategy,
    plateau_min_rounds=st.integers(min_value=1, max_value=3),
    min_relative_improvement=st.floats(min_value=0.0, max_value=0.2, allow_nan=False),
)
def test_ranked_bottlenecks_matches_old_controllers_ephemeral_ranking(
    events: list[_ProfileFocusEvent],
    plateau_min_rounds: int,
    min_relative_improvement: float,
) -> None:
    """Reference model of ``ProfileGuidedHypothesisController``'s ephemeral
    ``_ranking`` field (from ``loops/profile_multi/controller.py`` /
    ``loops/profile_single/hypothesis.py`` at commit 96ad78e0, reimplemented
    here, not imported): it holds exactly the tuple passed to the most
    recent ``prepare_round`` (``observe``) call, and is cleared to ``()``
    whenever ``advance_round`` (``record``) fully applies a measured, passing
    round to the active component. Drives ``ProfileFocus`` with the same
    event sequence and asserts ``focus().ranked_bottlenecks`` always matches.
    """
    search = ProfileFocus(
        ProfileFocusConfig(
            plateau_min_rounds=plateau_min_rounds, min_relative_improvement=min_relative_improvement
        )
    )
    state = search.initial()
    reference_ranking: tuple[ProfileBottleneck, ...] = ()

    for round_number, event in enumerate(events, start=1):
        if isinstance(event, _ObserveEvent):
            state = search.observe(state, round_number=round_number, bottlenecks=event.attribution)
            reference_ranking = event.attribution
        else:
            if (
                state.active_component is not None
                and event.passed
                and event.relative_improvement is not None
            ):
                reference_ranking = ()
            state = search.record(
                state,
                round_number=round_number,
                passed=event.passed,
                relative_improvement=event.relative_improvement,
            )
        assert search.focus(state).ranked_bottlenecks == reference_ranking


def test_observe_never_reselects_an_exhausted_component() -> None:
    search = ProfileFocus(ProfileFocusConfig(plateau_min_rounds=1, min_relative_improvement=0.5))
    state = search.initial()
    attribution = _bottleneck_tuple(["attn", "mlp"])
    state = search.observe(state, round_number=1, bottlenecks=attribution)
    active_before = state.active_component
    assert active_before is not None
    # A single round with a tiny improvement exhausts the active component
    # (plateau_min_rounds=1).
    state = search.record(state, round_number=1, passed=True, relative_improvement=0.0)
    exhausted = next(c for c in state.components if c.name == active_before)
    assert exhausted.status.value == "exhausted"

    for round_number in range(2, 6):
        state = search.observe(state, round_number=round_number, bottlenecks=attribution)
        assert state.active_component != active_before


def test_focus_renders_most_recently_observed_round_only() -> None:
    """``focus`` shows only the most recent ``observe`` call's attribution,
    matching the old controller's ephemeral ranking (which only ever held
    the round that just called ``prepare_round``, never a reconstruction
    across all rounds), rebuilt as a pure function of the persisted state
    via the observed round number instead of an in-memory field.
    """
    search = ProfileFocus(ProfileFocusConfig())
    state = search.initial()
    state = search.observe(state, round_number=1, bottlenecks=_bottleneck_tuple(["attn", "mlp"]))
    first_ranking = [item.name for item in search.focus(state).ranked_bottlenecks]
    assert first_ranking == ["attn", "mlp"]

    state = search.observe(state, round_number=2, bottlenecks=_bottleneck_tuple(["mlp", "attn"]))
    second_ranking = [item.name for item in search.focus(state).ranked_bottlenecks]
    assert second_ranking == ["mlp", "attn"]


def test_focus_drops_a_component_not_observed_in_the_most_recent_round() -> None:
    """Regression: a component observed in round 1 but absent from round 2's
    attribution must not appear in round 2's ranking. The buggy
    implementation reconstructed ``ranked_bottlenecks`` from each
    component's latest sample across all rounds, so a component observed
    once would linger in every later round's ranking even after it dropped
    out of the profiler's report.
    """
    search = ProfileFocus(ProfileFocusConfig())
    state = search.initial()
    state = search.observe(state, round_number=1, bottlenecks=_bottleneck_tuple(["attn", "mlp"]))
    assert {item.name for item in search.focus(state).ranked_bottlenecks} == {"attn", "mlp"}

    # Round 2's profiler run reports only "mlp"; "attn" drops out.
    state = search.observe(state, round_number=2, bottlenecks=_bottleneck_tuple(["mlp"]))
    second_ranking = [item.name for item in search.focus(state).ranked_bottlenecks]
    assert second_ranking == ["mlp"]
    assert "attn" not in second_ranking


def test_focus_ranking_persists_across_a_continued_round_with_no_new_observation() -> None:
    """A round that never calls ``observe`` again (an existing hypothesis is
    simply continued) still sees the previous round's ranking, matching the
    old controller's ephemeral field persisting across a ``guidance`` access
    that isn't preceded by a fresh ``prepare_round`` call.
    """
    search = ProfileFocus(ProfileFocusConfig())
    state = search.initial()
    state = search.observe(state, round_number=1, bottlenecks=_bottleneck_tuple(["attn", "mlp"]))
    ranking = [item.name for item in search.focus(state).ranked_bottlenecks]
    assert ranking == ["attn", "mlp"]
    # No observe() call for round 2 (hypothesis continues); ranking unchanged.
    assert [item.name for item in search.focus(state).ranked_bottlenecks] == ranking


def test_focus_ranking_clears_once_a_measured_round_advances_the_component() -> None:
    """Once ``record`` applies a measured, passing round to the active
    component, the ranking clears until the next ``observe`` call, matching
    the old controller resetting its ephemeral ranking field whenever
    ``advance_round`` fully applied an outcome.
    """
    search = ProfileFocus(ProfileFocusConfig(plateau_min_rounds=5, min_relative_improvement=0.5))
    state = search.initial()
    state = search.observe(state, round_number=1, bottlenecks=_bottleneck_tuple(["attn", "mlp"]))
    assert search.focus(state).ranked_bottlenecks

    state = search.record(state, round_number=1, passed=True, relative_improvement=0.01)
    assert search.focus(state).ranked_bottlenecks == ()


def test_parse_attribution_extracts_framed_payload_sorted_by_cost() -> None:
    payload = (
        '{"version": 1, "cost_unit": "ms", "components": ['
        '{"name": "b", "cost": 5.0, "share": 0.5, "evidence": []}, '
        '{"name": "a", "cost": 5.0, "share": 0.5, "evidence": []}, '
        '{"name": "c", "cost": 10.0, "share": 0.9, "evidence": ["x"]}]}'
    )
    output = f"noise before\n{_BEGIN}\n{payload}\n{_END}\nnoise after"
    parsed = parse_attribution(output)
    # Sorted by cost desc, ties by name.
    assert [component.name for component in parsed] == ["c", "a", "b"]


def test_parse_attribution_rejects_missing_and_duplicate_components() -> None:
    with pytest.raises(ProfileAttributionError, match="no result artifact"):
        parse_attribution("no framed payload here")

    duplicate = (
        '{"version": 1, "cost_unit": "ms", "components": ['
        '{"name": "a", "cost": 1.0, "share": 0.5, "evidence": []}, '
        '{"name": "a", "cost": 1.0, "share": 0.5, "evidence": []}]}'
    )
    output = f"{_BEGIN}\n{duplicate}\n{_END}"
    with pytest.raises(ProfileAttributionError, match="unique"):
        parse_attribution(output)
