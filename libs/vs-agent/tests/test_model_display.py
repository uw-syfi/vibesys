"""Readable names come from agentshim through ``vs_agent.api``; an unnamed model is never guessed."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api import harnesses, model_display, provider_display_name

_VETTED = harnesses()


def test_every_vetted_harness_is_named() -> None:
    for harness in _VETTED:
        shown = model_display(harness.provider, harness.model)
        assert shown is not None, harness
        assert shown.model_name.strip() == shown.model_name != ""
        assert shown.family != ""
        assert provider_display_name(harness.provider) != ""


def test_a_tier_comes_with_its_name() -> None:
    for harness in _VETTED:
        shown = model_display(harness.provider, harness.model)
        assert shown is not None
        assert (shown.tier is None) == (shown.tier_name is None)


def test_a_model_agentshim_does_not_name_has_no_display() -> None:
    assert model_display("claude", "claude-unheard-of-9") is None
    assert model_display("claude", None) is None


@given(st.text(max_size=30))
def test_a_name_is_never_guessed_from_an_unknown_id(model: str) -> None:
    shown = model_display("claude", model)
    if shown is not None:
        assert model.strip().lower().startswith(("claude-", "claude"))
