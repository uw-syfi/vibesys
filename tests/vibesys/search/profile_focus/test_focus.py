"""Equivalence and property tests for ``search.profile_focus``.

Compares against ``ProfileGuidedHypothesisController`` and friends in
``loops/profile_multi/controller.py``, and ``parse_attribution`` against the
pure half of ``loops/profile_multi/attribution.py``. Once the rewiring phase
deletes ``loops/profile_multi/controller.py``, the ``test_*_matches_old_*``
tests should be trimmed to plain unit tests of ``search.profile_focus`` alone.
"""

from __future__ import annotations

import json

from hypothesis import given
from hypothesis import strategies as st

from vibesys.agent_run.state import AgentRunState
from vibesys.loops.profile_multi import attribution as old_attribution
from vibesys.loops.profile_multi.controller import ProfileGuidedHypothesisController
from vibesys.search.profile_focus import (
    ProfileBottleneck,
    ProfileFocus,
    ProfileFocusConfig,
    ProfileFocusState,
    parse_attribution,
)

_NAMES = st.sampled_from(["attn", "mlp", "kv_cache", "norm"])


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
def test_observe_and_record_match_old_controller(
    rounds: list[tuple[ProfileBottleneck, ...]],
    plateau_min_rounds: int,
    min_relative_improvement: float,
    improvements: list[float | None],
) -> None:
    old = ProfileGuidedHypothesisController.create(
        AgentRunState(),
        enabled=True,
        plateau_min_rounds=plateau_min_rounds,
        min_relative_improvement=min_relative_improvement,
    )
    new_search = ProfileFocus(
        ProfileFocusConfig(
            plateau_min_rounds=plateau_min_rounds, min_relative_improvement=min_relative_improvement
        )
    )
    new_state = new_search.initial()

    for round_number, attribution in enumerate(rounds, start=1):
        old = old.prepare_round(round_number=round_number, attribution=attribution)
        new_state = new_search.observe(
            new_state, round_number=round_number, bottlenecks=attribution
        )
        assert (
            old.state.profile_guidance or ProfileFocusState()
        ).model_dump() == new_state.model_dump()

        improvement = (
            improvements[round_number - 1] if round_number - 1 < len(improvements) else None
        )
        passed = improvement is not None
        old = old.advance_round(
            round_number=round_number, passed=passed, relative_improvement=improvement
        )
        new_state = new_search.record(
            new_state, round_number=round_number, passed=passed, relative_improvement=improvement
        )
        assert (
            old.state.profile_guidance or ProfileFocusState()
        ).model_dump() == new_state.model_dump()


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


# --- parse_attribution equivalence ---


def _framed(payload: dict) -> str:
    begin = old_attribution._BEGIN  # noqa: SLF001  # comparing framing against the old module's own markers
    end = old_attribution._END  # noqa: SLF001
    return f"noise before\n{begin}\n{json.dumps(payload)}\n{end}\nnoise after"


def test_parse_attribution_matches_old_framed_payload_and_sort() -> None:
    payload = {
        "version": 1,
        "cost_unit": "ms",
        "components": [
            {"name": "b", "cost": 5.0, "share": 0.5, "evidence": []},
            {"name": "a", "cost": 5.0, "share": 0.5, "evidence": []},
            {"name": "c", "cost": 10.0, "share": 0.9, "evidence": ["x"]},
        ],
    }
    output = _framed(payload)
    old_payload = old_attribution._framed_payload(output)  # noqa: SLF001
    assert old_payload is not None
    new_payload = parse_attribution(output)
    old_parsed = old_attribution._ProfileResultV1.model_validate_json(  # noqa: SLF001
        old_payload, strict=True
    )
    old_sorted = tuple(sorted(old_parsed.components, key=lambda item: (-item.cost, item.name)))
    assert [c.model_dump() for c in old_sorted] == [c.model_dump() for c in new_payload]
    # Sorted by cost desc, ties by name.
    assert [c.name for c in new_payload] == ["c", "a", "b"]
