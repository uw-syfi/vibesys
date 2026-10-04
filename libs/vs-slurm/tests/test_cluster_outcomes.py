"""Cluster schemas cannot serialize uncertainty as successful evidence."""

from __future__ import annotations

import json
import string

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vs_slurm.api import (
    ClusterCollected,
    ClusterObservation,
    ClusterUnknown,
    SlurmBatchResult,
    SlurmBatchStageResult,
    SlurmJobResult,
    SlurmJobStatus,
)


@pytest.mark.parametrize("status", list(SlurmJobStatus))
def test_scheduler_status_has_exactly_one_observation_representation(
    status: SlurmJobStatus,
) -> None:
    values = {"operation_id": "operation", "job_id": "42", "status": status}
    if status is SlurmJobStatus.UNKNOWN:
        with pytest.raises(ValidationError):
            ClusterObservation.model_validate(values)
    else:
        observed = ClusterObservation.model_validate(values)
        assert ClusterObservation.model_validate_json(observed.model_dump_json()) == observed


@pytest.mark.parametrize("exit_code", [None, 0, 7, -1, 256, False])
@pytest.mark.parametrize("collection_failure", [None, "missing artifact"])
def test_collected_job_requires_known_status_and_complete_evidence(
    exit_code: int | None, collection_failure: str | None
) -> None:
    result = SlurmJobResult(
        job_id="42", exit_code=exit_code, output="evidence", collection_failure=collection_failure
    )
    values = {"operation_id": "operation", "result": result}
    if (
        exit_code is None
        or isinstance(exit_code, bool)
        or not 0 <= exit_code <= 255
        or collection_failure
    ):
        with pytest.raises(ValidationError):
            ClusterCollected.model_validate(values)
    else:
        collected = ClusterCollected.model_validate(values)
        assert ClusterCollected.model_validate_json(collected.model_dump_json()) == collected


@pytest.mark.parametrize("exit_code", [None, 0, 7, -1, 256, False])
@pytest.mark.parametrize("collection_failure", [None, "missing artifact"])
def test_collected_batch_requires_known_stage_status_and_complete_evidence(
    exit_code: int | None, collection_failure: str | None
) -> None:
    result = SlurmBatchResult(
        job_id="42",
        job_exit_code=0,
        job_output="",
        phase_timings_seconds={},
        content_cache_hits=0,
        stages=(
            SlurmBatchStageResult(
                name="stage",
                exit_code=exit_code,
                stdout="evidence",
                stderr="",
                elapsed_seconds=0.0,
                skipped=False,
                collection_failure=collection_failure,
            ),
        ),
    )
    values = {"operation_id": "operation", "result": result}
    if (
        exit_code is None
        or isinstance(exit_code, bool)
        or not 0 <= exit_code <= 255
        or collection_failure
    ):
        with pytest.raises(ValidationError):
            ClusterCollected.model_validate(values)
    else:
        collected = ClusterCollected.model_validate(values)
        assert ClusterCollected.model_validate_json(collected.model_dump_json()) == collected


@given(
    key=st.text(alphabet=string.ascii_letters, min_size=1, max_size=12).filter(
        lambda key: key not in {"operation_id", "reason", "job_id", "result", "kind"}
    )
)
def test_unknown_metadata_keys_cannot_enter_an_outcome(key: str) -> None:
    with pytest.raises(ValidationError, match=key):
        ClusterUnknown.model_validate({"operation_id": "operation", "reason": "reply lost", key: 7})


@pytest.mark.parametrize("value", [False, True, "0", "7", 0.0, 7.0, -1, 256, None])
@pytest.mark.parametrize("location", ["job", "allocation", "stage"])
def test_collected_json_rejects_noninteger_or_missing_exit_status(
    value: object, location: str
) -> None:
    if location == "job":
        result: dict[str, object] = {"job_id": "42", "exit_code": value, "output": "evidence"}
    else:
        result = {
            "job_id": "42",
            "job_exit_code": value if location == "allocation" else 0,
            "job_output": "",
            "phase_timings_seconds": {},
            "content_cache_hits": 0,
            "stages": [
                {
                    "name": "stage",
                    "exit_code": value if location == "stage" else 0,
                    "stdout": "evidence",
                    "stderr": "",
                    "elapsed_seconds": 0.0,
                    "skipped": False,
                }
            ],
        }
    with pytest.raises(ValidationError):
        ClusterCollected.model_validate_json(
            json.dumps({"operation_id": "operation", "result": result})
        )
