"""Strict suspension-bound configuration through the public config schema."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from vibesys.config import Config
from vs_core.api import MeasurementFailure
from vs_sim.api.testing import examples


def test_queue_allowance_defaults_to_fifteen_minutes() -> None:
    assert Config(model={"name": "test"}).evaluation.queue_allowance_seconds == 900


@settings(max_examples=examples(pr=8, nightly=100))
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


def _evaluation(**settings: object) -> Config:
    return Config.model_validate({"model": {"name": "test"}, "evaluation": settings})


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    infrastructure=st.integers(min_value=1, max_value=5),
    ambiguous=st.integers(min_value=1, max_value=5),
    limit=st.integers(min_value=0, max_value=10),
)
def test_the_backend_retry_declarations_reach_the_run_exactly_as_configured(
    infrastructure: int, ambiguous: int, limit: int
) -> None:
    if ambiguous > infrastructure:
        with pytest.raises(ValidationError, match="ambiguous_attempts"):
            _evaluation(infrastructure_attempts=infrastructure, ambiguous_attempts=ambiguous)
        return
    evaluation = _evaluation(
        infrastructure_attempts=infrastructure,
        ambiguous_attempts=ambiguous,
        request_retry_limit=limit,
    ).evaluation
    assert evaluation.evaluation_retries().infrastructure_attempts == infrastructure
    assert evaluation.evaluation_retries().ambiguous_attempts == ambiguous
    assert evaluation.request_retries().limit == limit


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("infrastructure_attempts", 6),
        ("infrastructure_attempts", 0),
        ("request_retry_limit", 11),
        ("request_backoff_seconds", 0),
    ],
)
def test_a_retry_bound_beyond_what_core_allows_is_refused_at_load(
    setting: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        _evaluation(**{setting: value})


def test_a_default_run_measures_an_ambiguous_failure_twice_and_a_lost_job_three_times() -> None:
    """An evaluator killed with no result record is measured once more, then it is final.

    The classification of a failure (libs/vs-runtime) only says it is ambiguous; how often an
    ambiguous or an infrastructure failure is measured is this declaration, so the bounds an
    operator gets without configuring anything are pinned here.
    """
    retries = Config(model={"name": "test"}).evaluation.evaluation_retries()

    assert (retries.infrastructure_attempts, retries.ambiguous_attempts) == (3, 2)
    assert retries.allows_another(MeasurementFailure.AMBIGUOUS, tries=1)
    assert not retries.allows_another(MeasurementFailure.AMBIGUOUS, tries=2)
    assert retries.allows_another(MeasurementFailure.INFRASTRUCTURE, tries=2)
    assert not retries.allows_another(MeasurementFailure.INFRASTRUCTURE, tries=3)
    assert not retries.allows_another(MeasurementFailure.WORKLOAD, tries=1)
