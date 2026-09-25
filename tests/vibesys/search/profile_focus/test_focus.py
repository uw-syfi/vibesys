"""Unit and property tests for ``search.profile_focus``."""

from __future__ import annotations

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


def test_focus_renders_latest_observed_ranking() -> None:
    """``focus`` shows each component's latest attribution sample, matching
    the old controller's ephemeral ranking (which only ever held the round
    that just called ``prepare_round``), rebuilt as a pure function of the
    persisted state instead. A profiler run reports every tracked component
    each round, so re-observing the same components with a new ordering
    replaces the prior round's ranking rather than accumulating alongside it.
    """
    search = ProfileFocus(ProfileFocusConfig())
    state = search.initial()
    state = search.observe(state, round_number=1, bottlenecks=_bottleneck_tuple(["attn", "mlp"]))
    first_ranking = [item.name for item in search.focus(state).ranked_bottlenecks]
    assert first_ranking == ["attn", "mlp"]

    state = search.observe(state, round_number=2, bottlenecks=_bottleneck_tuple(["mlp", "attn"]))
    second_ranking = [item.name for item in search.focus(state).ranked_bottlenecks]
    assert second_ranking == ["mlp", "attn"]


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
