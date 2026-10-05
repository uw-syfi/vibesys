"""Strict whole-run bound configuration through the public config schema."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vibesys.config import Config


def test_max_run_seconds_is_unbounded_by_default() -> None:
    assert Config(model={"name": "test"}).run.max_run_seconds is None


@given(st.integers(min_value=1))
def test_max_run_seconds_preserves_positive_integer(seconds: int) -> None:
    config = Config.model_validate({"model": {"name": "test"}, "run": {"max_run_seconds": seconds}})
    assert config.run.max_run_seconds == seconds


@pytest.mark.parametrize("value", [0, -1, True, "60", 60.0, float("inf")])
def test_max_run_seconds_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValidationError, match="max_run_seconds"):
        Config.model_validate({"model": {"name": "test"}, "run": {"max_run_seconds": value}})


def test_run_rejects_unknown_settings() -> None:
    with pytest.raises(ValidationError, match="max_run_second"):
        Config.model_validate({"model": {"name": "test"}, "run": {"max_run_second": 60}})


def _evaluation(**values: object) -> Config:
    return Config.model_validate({"model": {"name": "test"}, "evaluation": values})


def test_observe_pacing_defaults() -> None:
    evaluation = Config(model={"name": "test"}).evaluation
    assert (evaluation.observe_interval_seconds, evaluation.observe_backoff_cap_seconds) == (
        10,
        120,
    )


@given(st.integers(min_value=1, max_value=10**6), st.integers(min_value=0, max_value=10**6))
def test_observe_pacing_preserves_a_cap_that_covers_the_interval(interval: int, extra: int) -> None:
    evaluation = _evaluation(
        observe_interval_seconds=interval, observe_backoff_cap_seconds=interval + extra
    ).evaluation
    assert (evaluation.observe_interval_seconds, evaluation.observe_backoff_cap_seconds) == (
        interval,
        interval + extra,
    )


@pytest.mark.parametrize("field", ["observe_interval_seconds", "observe_backoff_cap_seconds"])
@pytest.mark.parametrize("value", [0, -1, True, "5", 1.5])
def test_observe_pacing_rejects_invalid_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match=field):
        _evaluation(**{field: value})


def test_observe_cap_below_interval_is_rejected_by_name() -> None:
    with pytest.raises(ValidationError, match="observe_backoff_cap_seconds"):
        _evaluation(observe_interval_seconds=30, observe_backoff_cap_seconds=20)
