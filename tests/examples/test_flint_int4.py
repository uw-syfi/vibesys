"""Quantized task identity and the CPU reference's public probability parser."""

from __future__ import annotations

import json
import runpy
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.examples.example_task import load_example_task, task_root

_PROJECT = Path(__file__).parents[2] / "examples/model-serving/flint"
_CHECKER = SimpleNamespace(**runpy.run_path(str(_PROJECT / ".vibesys/shared/int4_checker.py")))


def test_int4_task_pins_quantized_weights_and_base_tokenizer() -> None:
    manifest = load_example_task(_PROJECT, "mi2101x-int4").manifest
    assert manifest.catalogue is not None
    assert manifest.catalogue.hardware.accelerator == "AMD MI210"
    assert manifest.catalogue.hardware.count == 1
    assert manifest.benchmark.entrypoint == "agentx-v1"
    args = manifest.benchmark.args
    assert args[args.index("--model") + 1] == _CHECKER.MODEL
    assert args[args.index("--tokenizer") + 1] == "RecursiveSI/flint"
    assert (
        args[args.index("--tokenizer-revision") + 1] == "2a247be0c0aec895ccb0ac7ca92229ec6df37288"
    )
    assert manifest.accuracy.command is not None
    assert manifest.accuracy.command[-1].endswith("int4_checker.py")
    assert "--locked" in manifest.accuracy.command
    assert "--isolated" in manifest.accuracy.command
    root = task_root(_PROJECT, "mi2101x-int4")
    assert json.loads((root / "reference/meta.json").read_text()) == {
        "model_id": _CHECKER.MODEL,
        "revision": _CHECKER.REVISION,
    }
    objectives = tomllib.loads((root / "objectives.toml").read_text())
    assert len(objectives["objective"]) == 5
    assert objectives["pareto"]["relative_noise"] == 0.03


def _payload(best: float, chosen: float) -> dict:
    return {
        "completion_probabilities": [
            {
                "id": 0,
                "logprob": best,
                "top_logprobs": [{"id": 0, "logprob": best}, {"id": 1, "logprob": chosen}],
            }
        ]
    }


@given(best=st.floats(-100, -1, allow_nan=False), gap=st.floats(0, 10, allow_nan=False))
def test_reference_parser_preserves_chosen_and_best_logprobs(best: float, gap: float) -> None:
    chosen = best - gap
    assert _CHECKER.parse_reference_step(_payload(best, chosen), 1) == (chosen, best)


@pytest.mark.parametrize("value", [None, True, "-1", 0.1, float("nan"), float("inf")])
def test_reference_parser_rejects_invalid_probabilities(value: object) -> None:
    payload = _payload(-1.0, -1.1)
    payload["completion_probabilities"][0]["top_logprobs"][1]["logprob"] = value
    with pytest.raises(ValueError, match="logprob"):
        _CHECKER.parse_reference_step(payload, 1)


@given(value=st.one_of(st.booleans(), st.integers(max_value=-1), st.integers(min_value=150144)))
def test_reference_parser_rejects_invalid_token_ids(value: object) -> None:
    payload = _payload(-1.0, -1.1)
    payload["completion_probabilities"][0]["top_logprobs"][1]["id"] = value
    with pytest.raises(ValueError, match="id"):
        _CHECKER.parse_reference_step(payload, 1)


def test_reference_parser_fails_closed_outside_top_k() -> None:
    with pytest.raises(ValueError, match="outside CPU reference top-32"):
        _CHECKER.parse_reference_step(_payload(-1.0, -1.1), 2)


@pytest.mark.parametrize("payload", [None, {}, {"completion_probabilities": []}])
def test_reference_parser_rejects_missing_observations(payload: object) -> None:
    with pytest.raises(ValueError, match=r"CPU completion|completion_probabilities"):
        _CHECKER.parse_reference_step(payload, 1)


def test_reference_parser_rejects_conflicting_duplicate_probabilities() -> None:
    payload = _payload(-1.0, -1.1)
    payload["completion_probabilities"][0]["top_logprobs"].append({"id": 1, "logprob": -2.0})
    with pytest.raises(ValueError, match="inconsistent"):
        _CHECKER.parse_reference_step(payload, 1)


def test_reference_parser_requires_a_greedy_reference() -> None:
    with pytest.raises(ValueError, match="reference maximum"):
        _CHECKER.parse_reference_step(_payload(-1.1, -1.0), 1)
