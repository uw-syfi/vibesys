"""Every model VibeSys vets has a price in agentshim, so its turns record a cost."""

from __future__ import annotations

import agentshim
import pytest

from vs_agent.api import Harness, harnesses

_ONE_MILLION = agentshim.TokenUsage(
    input_tokens=1_000_000, cache_read_input_tokens=0, output_tokens=1_000_000
)

#: Vetted models agentshim has no price for, and why. A new vetted model must be priced
#: or named here, so it cannot reach a run with its cost reading ``unknown`` unnoticed.
_UNPRICED = {
    ("opencode", "google-vertex/gemini-3-pro-preview"): (
        "Google's pricing page no longer lists it, so agentshim has no official number"
    ),
}


@pytest.mark.parametrize(
    "harness", harnesses(), ids=lambda harness: f"{harness.provider}/{harness.model}"
)
def test_every_vetted_harness_is_priced_and_a_turn_costs_something(harness: Harness) -> None:
    """Regression: ``gpt-6.1-sol`` was vetted but unpriced, so its turns recorded no cost.

    A paid replay turn of an unpriced model reads ``unknown`` instead of an amount.
    """
    pricing = agentshim.price_for(harness.provider, harness.model)

    if (harness.provider, harness.model) in _UNPRICED:
        assert pricing is None, "now priced: drop it from _UNPRICED"
        return
    assert pricing is not None, f"agentshim has no price for {harness.provider}/{harness.model}"
    assert agentshim.cost_usd(_ONE_MILLION, pricing) > 0


def test_gpt_6_1_sol_is_priced_at_its_own_cache_read_rate() -> None:
    sol = agentshim.price_for("codex", "gpt-6.1-sol")
    older = agentshim.price_for("codex", "gpt-6-sol")

    assert sol is not None
    assert older is not None
    assert sol.cache_read_usd != older.cache_read_usd
    cached = agentshim.TokenUsage(input_tokens=1_000_000, cache_read_input_tokens=1_000_000)
    assert agentshim.cost_usd(cached, sol) == pytest.approx(0.10)
