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
