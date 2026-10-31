"""Journal lines of finished agent turns that carry the per-model split, as a producer could write them."""

from __future__ import annotations

from typing import Any

from hypothesis import strategies as st

MODELS = ("opus", "opus[1m]", "haiku", "sonnet", "gpt-6.1-sol", "vendor/odd", "mystery")
SPEEDS = (None, "standard", "fast", "ultrafast")
BASES = ("price_table", "provider_reported", "unknown_model", "not_reported")


@st.composite
def model_row(draw: st.DrawFn, model: str, *, primary: bool, turns: int) -> dict[str, Any]:
    """One journal row under the library's invariants: cache within input, reasoning within output."""
    input_tokens = draw(st.integers(0, 10**7))
    output_tokens = draw(st.integers(0, 10**6))
    cache_read = draw(st.integers(0, input_tokens))
    cache_write = draw(st.integers(0, input_tokens - cache_read))
    return {
        "model": model,
        "input_tokens": input_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
        "cache_creation_1h_input_tokens": draw(st.integers(0, cache_write)),
        "output_tokens": output_tokens,
        "reasoning_output_tokens": draw(st.integers(0, output_tokens)),
        "turns": turns if primary else 0,
        "recorded_usd": draw(st.none() | st.floats(0, 1000, allow_nan=False, allow_infinity=False)),
        "speed": draw(st.sampled_from(SPEEDS)) if primary else None,
        "primary": primary,
    }


@st.composite
def split_usage_lines(draw: st.DrawFn) -> dict[str, Any]:
    """A ``turn_usage_recorded`` journal event whose data splits the turn by model."""
    models = draw(st.lists(st.sampled_from(MODELS), min_size=1, max_size=4, unique=True))
    primary = draw(st.integers(0, len(models) - 1))
    turns = draw(st.integers(1, 40))
    rows = [
        draw(model_row(model, primary=index == primary, turns=turns))
        for index, model in enumerate(models)
    ]
    return usage_line(
        {
            "input_tokens": rows[primary]["input_tokens"],
            "total_cost_usd": draw(st.none() | st.floats(0, 1000, allow_nan=False)),
            "cost_basis": draw(st.sampled_from((*BASES, None))),
            "by_model": rows,
            "provider_session_id": draw(st.none() | st.text(min_size=1, max_size=12)),
        }
    )


def usage_line(data: dict[str, Any], *, sequence: int = 1) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "timestamp": "2026-10-30T09:00:00Z",
        "type": "turn_usage_recorded",
        "data": {"kind": "turn_usage_recorded", **data},
    }
