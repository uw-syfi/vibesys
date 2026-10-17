"""Every vetted model reads the same way in every frontend: family, tier and a readable name."""

from __future__ import annotations

from collections import defaultdict

from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api import harnesses, model_display

_VETTED = harnesses()


def test_every_vetted_model_is_described() -> None:
    undescribed = [h for h in _VETTED if model_display(h.provider, h.model) is None]
    assert undescribed == []


def test_a_family_belongs_to_one_provider_and_its_tiers_are_a_dense_ranking() -> None:
    tiers: dict[str, list[int]] = defaultdict(list)
    providers: dict[str, set[str]] = defaultdict(set)
    for harness in _VETTED:
        shown = model_display(harness.provider, harness.model)
        assert shown is not None
        tiers[shown.family].append(shown.tier)
        providers[shown.family].add(harness.provider)
    assert all(len(owners) == 1 for owners in providers.values())
    assert all(sorted(ranks) == list(range(len(ranks))) for ranks in tiers.values())


def test_names_are_readable_and_distinct() -> None:
    shown = [model_display(h.provider, h.model) for h in _VETTED]
    names = [each.model_name for each in shown if each is not None]
    assert len(names) == len(set(names))
    for each in shown:
        assert each is not None
        for text in (each.provider_name, each.model_name, each.family_name, each.tier_name):
            assert text != ""
            assert text == text.strip()
            assert "_" not in text


@given(st.text(max_size=20), st.text(max_size=20))
def test_a_pair_that_is_not_vetted_is_not_described(provider: str, model: str) -> None:
    vetted = {(h.provider, h.model) for h in _VETTED}
    if (provider, model) not in vetted:
        assert model_display(provider, model) is None
