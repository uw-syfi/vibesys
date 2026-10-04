"""Strict suspension-bound configuration through the public config schema."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vibesys.config import Config


def test_queue_allowance_defaults_to_fifteen_minutes() -> None:
    assert Config(model={"name": "test"}).evaluation.queue_allowance_seconds == 900


@given(st.integers(min_value=1))
def test_queue_allowance_preserves_positive_integer(seconds: int) -> None:
    config = Config.model_validate(
        {"model": {"name": "test"}, "evaluation": {"queue_allowance_seconds": seconds}}
    )
    assert config.evaluation.queue_allowance_seconds == seconds


@pytest.mark.parametrize("value", [0, -1, True, "900", 900.0, None])
def test_queue_allowance_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValidationError, match="queue_allowance_seconds"):
        Config.model_validate(
            {"model": {"name": "test"}, "evaluation": {"queue_allowance_seconds": value}}
        )


def test_evaluation_rejects_unknown_settings() -> None:
    with pytest.raises(ValidationError, match="queue_allowance_second"):
        Config.model_validate(
            {"model": {"name": "test"}, "evaluation": {"queue_allowance_second": 900}}
        )
