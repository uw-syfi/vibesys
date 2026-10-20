"""The container's Claude Code must know every model agentshim lists, and cost follows agentshim."""

from __future__ import annotations

import json
from typing import Any

import agentshim
import pytest
from agentshim.providers.claude import ClaudeStreamParser
from hypothesis import given
from hypothesis import strategies as st

from vs_agent import shim_translation
from vs_agent.api import CLAUDE_CLI_MIN_FOR_GENERATION, CLI_VERSIONS


def _numbers(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _claude_generations() -> dict[str, int]:
    """Each Claude generation agentshim lists, by ``generation_id``, with its rank (1 newest)."""
    ranks: dict[str, int] = {}
    for spec in agentshim.default_models().entries:
        if spec.vendor == "anthropic" and spec.generation_id and spec.generation_rank:
            ranks[spec.generation_id] = spec.generation_rank
    return ranks


def test_the_newest_claude_generation_agentshim_lists_has_a_minimum_cli_version() -> None:
    """A new model in agentshim must come with the CLI release that knows it.

    Regression: Claude Code 2.1.268 did not know ``claude-sonnet-5-5``, so every
    session printed ``unrecognized_model`` and the CLI billed it at a fallback rate.
    """
    ranks = _claude_generations()
    newest = min(ranks, key=lambda generation: ranks[generation])
    assert newest in CLAUDE_CLI_MIN_FOR_GENERATION, (
        f"agentshim lists Claude {newest}; add the first Claude Code release that knows "
        "all its models to provider_policy.CLAUDE_CLI_MIN_FOR_GENERATION and pin at least it"
    )


def test_the_pinned_claude_cli_knows_every_generation_the_policy_names() -> None:
    pinned = _numbers(CLI_VERSIONS["claude"])
    too_old = {
        generation: minimum
        for generation, minimum in CLAUDE_CLI_MIN_FOR_GENERATION.items()
        if _numbers(minimum) > pinned
    }
    assert not too_old, f"CLI_VERSIONS['claude'] is older than the release that knows {too_old}"


def test_every_named_generation_is_one_agentshim_lists() -> None:
    assert set(CLAUDE_CLI_MIN_FOR_GENERATION) <= set(_claude_generations())


def _result(models: dict[str, Any], cli_cost: float) -> str:
    usage = {
        "input_tokens": 1,
        "cache_creation_input_tokens": 6000,
        "cache_creation": {"ephemeral_1h_input_tokens": 6000, "ephemeral_5m_input_tokens": 0},
    }
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "result": "ok",
            "total_cost_usd": cli_cost,
            "usage": usage,
            "modelUsage": models,
        }
    )


def _recorded_cost(models: dict[str, Any], cli_cost: float) -> float | None:
    parser = ClaudeStreamParser(lambda _event: None)
    parser.feed_stdout(_result(models, cli_cost) + "\n")
    parsed = parser.finish()
    return shim_translation.usage_from(parsed.usage, cost_usd=parsed.cost_usd).total_cost_usd


_PROBE = {
    "claude-sonnet-5-5": {
        "inputTokens": 6,
        "outputTokens": 2000,
        "cacheReadInputTokens": 19_860,
        "cacheCreationInputTokens": 6000,
    }
}


def test_recorded_turn_cost_is_agentshims_not_an_old_clis_overstatement() -> None:
    """The probe: Claude Code 2.1.268 reported 0.11996 for what Sonnet 5.5 prices at about 0.046."""
    assert _recorded_cost(_PROBE, cli_cost=0.11996) == pytest.approx(0.045998)


@given(cli_cost=st.floats(min_value=0, max_value=1_000))
def test_the_cli_figure_never_reaches_the_recorded_cost_of_a_known_model(cli_cost: float) -> None:
    assert _recorded_cost(_PROBE, cli_cost=cli_cost) == pytest.approx(0.045998)


@given(cli_cost=st.floats(min_value=0, max_value=1_000))
def test_a_model_agentshim_cannot_price_records_no_cost_not_the_clis_guess(cli_cost: float) -> None:
    unknown = {"claude-not-yet-priced-9": _PROBE["claude-sonnet-5-5"]}
    assert _recorded_cost(unknown, cli_cost=cli_cost) is None
