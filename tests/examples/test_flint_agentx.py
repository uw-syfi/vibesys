"""Flint hardware variants and the numerical checker's public validation API."""

from __future__ import annotations

import runpy
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.examples.example_task import load_example_task, task_root

if TYPE_CHECKING:
    from collections.abc import Sequence

_PROJECT = Path(__file__).parents[2] / "examples/model-serving/flint"
_REVISION = "2a247be0c0aec895ccb0ac7ca92229ec6df37288"
_CHECKER = SimpleNamespace(**runpy.run_path(str(_PROJECT / ".vibesys/shared/checker.py")))


def test_hardware_variants_share_the_agentx_recipe_and_reference() -> None:
    small = load_example_task(_PROJECT, "mi2104x").manifest
    large = load_example_task(_PROJECT, "mi300x").manifest
    assert small.catalogue is not None
    assert large.catalogue is not None
    assert small.catalogue.hardware.accelerator == "AMD MI210"
    assert small.catalogue.hardware.count == 4
    assert large.catalogue.hardware.accelerator == "AMD MI300X"
    assert large.catalogue.hardware.count == 1
    assert small.benchmark == large.benchmark
    assert small.accuracy == large.accuracy
    assert small.benchmark.entrypoint == "agentx-v1"
    assert small.benchmark.result_protocol == 2
    assert _REVISION in small.benchmark.args
    assert small.benchmark.args[-4:] == ("--concurrency", "4", "--max-context-length", "262144")
    for task in ("mi2104x", "mi300x"):
        root = task_root(_PROJECT, task)
        assert (root / "reference/meta.json").read_text() == (
            task_root(_PROJECT, "mi2104x") / "reference/meta.json"
        ).read_text()
        assert (root / "objectives.toml").read_text() == (
            task_root(_PROJECT, "mi2104x") / "objectives.toml"
        ).read_text()


def _payload(ids: Sequence[object], values: Sequence[object], endpoint: str) -> dict[str, object]:
    probabilities = (
        {"token_logprobs": list(values)}
        if endpoint == "completions"
        else {"content": [{"logprob": value} for value in values]}
    )
    return {
        "choices": [{"token_ids": list(ids), "logprobs": probabilities}],
        "usage": {"prompt_tokens": 513, "completion_tokens": 8},
    }


@given(
    ids=st.lists(st.integers(0, 999), min_size=8, max_size=8),
    values=st.lists(st.floats(-100, 0, allow_nan=False), min_size=8, max_size=8),
    endpoint=st.sampled_from(("completions", "chat")),
)
def test_completion_preserves_valid_observations(
    ids: list[int], values: list[float], endpoint: Literal["completions", "chat"]
) -> None:
    result = _CHECKER.parse_completion(
        _payload(ids, values, endpoint), 513, 1000, endpoint=endpoint
    )
    assert result.ids == tuple(ids)
    assert result.logprobs == tuple(values)


@given(value=st.one_of(st.booleans(), st.integers(max_value=-1), st.integers(min_value=1000)))
def test_completion_rejects_invalid_token_ids(value: object) -> None:
    payload = _payload([1] * 8, [-1.0] * 8, "completions")
    choices = payload["choices"]
    assert isinstance(choices, list)
    choices[0]["token_ids"][0] = value
    with pytest.raises(ValueError, match="token_ids"):
        _CHECKER.parse_completion(payload, 513, 1000)


@pytest.mark.parametrize("value", [True, None, "-1", float("nan"), float("inf"), 0.1])
@pytest.mark.parametrize("endpoint", ["completions", "chat"])
def test_completion_rejects_invalid_probabilities(value: object, endpoint: str) -> None:
    payload = _payload([1] * 8, [value] * 8, endpoint)
    with pytest.raises(ValueError, match="logprobs"):
        _CHECKER.parse_completion(payload, 513, 1000, endpoint=endpoint)


@pytest.mark.parametrize("count", [True, 512, 514, "513", None])
def test_completion_requires_exact_usage(count: object) -> None:
    payload = _payload([1] * 8, [-1.0] * 8, "completions")
    payload["usage"] = {"prompt_tokens": count, "completion_tokens": 8}
    with pytest.raises(ValueError, match="usage"):
        _CHECKER.parse_completion(payload, 513, 1000)


@pytest.mark.parametrize("payload", [None, [], {}, {"choices": []}])
def test_completion_rejects_missing_observations(payload: object) -> None:
    with pytest.raises(ValueError, match=r"JSON object|choices"):
        _CHECKER.parse_completion(payload, 513, 1000)


@given(value=st.floats(-100, -1, allow_nan=False), gap=st.floats(0, 0.049))
def test_numerical_gate_accepts_near_ties(value: float, gap: float) -> None:
    completion = _CHECKER.Completion((1,) * 8, (value,) * 8)
    _CHECKER.assess(completion, [_CHECKER.ReferenceStep(value, value + gap)] * 8)


@pytest.mark.parametrize(
    ("chosen", "best", "actual"), [(-1.2, -1, -1.2), (-1, -1, -1.06), (-1, -1, -0.8)]
)
def test_numerical_gate_rejects_greedy_or_probability_drift(
    chosen: float, best: float, actual: float
) -> None:
    with pytest.raises(ValueError, match=r"greedy mismatch|error budget"):
        _CHECKER.assess(
            _CHECKER.Completion((1,) * 8, (actual,) * 8),
            [_CHECKER.ReferenceStep(chosen, best)] * 8,
        )


def test_numerical_gate_enforces_per_token_budget_even_with_small_mean() -> None:
    with pytest.raises(ValueError, match="error budget"):
        _CHECKER.assess(
            _CHECKER.Completion((1,) * 8, (-1.2,) + (-1.0,) * 7),
            [_CHECKER.ReferenceStep(-1.0, -1.0)] * 8,
        )
