"""Whose fault a failed benchmark is: one table, total, and never lossy."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import MeasurementFailure, may_resubmit
from vs_runtime.api import BenchmarkFailureKind
from vs_runtime.api.infrastructure import (
    RecordState,
    TerminalSignal,
    classify,
    job_failure,
    signal_of,
)

_KINDS = (
    BenchmarkFailureKind.WORKLOAD,
    BenchmarkFailureKind.INFRASTRUCTURE,
    BenchmarkFailureKind.AMBIGUOUS,
)
_BOUND = st.integers(min_value=1, max_value=8)
_SIGNALS = st.sampled_from(TerminalSignal)
_RECORDS = st.sampled_from(RecordState)


@given(signal=_SIGNALS, record=_RECORDS)
def test_every_signal_and_record_has_a_class(signal: TerminalSignal, record: RecordState) -> None:
    assert classify(signal, record) in _KINDS


@given(exit_code=st.none() | st.integers(min_value=-300, max_value=300), ready=st.booleans())
def test_every_exit_status_reads_as_a_signal(exit_code: int | None, *, ready: bool) -> None:
    assert signal_of(exit_code, service_not_ready=not ready) in set(TerminalSignal)


@given(record=_RECORDS)
def test_a_job_the_machinery_lost_is_never_the_candidates(record: RecordState) -> None:
    assert classify(signal_of(None), record) is BenchmarkFailureKind.INFRASTRUCTURE
    assert classify(signal_of(-9), record) is BenchmarkFailureKind.INFRASTRUCTURE


@given(exit_code=st.integers(min_value=0, max_value=255))
def test_a_stage_that_left_an_outcome_record_is_the_candidates(exit_code: int) -> None:
    assert classify(signal_of(exit_code), RecordState.OUTCOME) is BenchmarkFailureKind.WORKLOAD


@given(exit_code=st.integers(min_value=0, max_value=255))
def test_a_process_that_died_with_no_record_is_retried_at_most_once(exit_code: int) -> None:
    kind = classify(signal_of(exit_code), RecordState.ABSENT)
    assert kind is BenchmarkFailureKind.AMBIGUOUS


_CANDIDATE_LOGS = (
    "torch.OutOfMemoryError: CUDA out of memory",
    "HIP error: out of memory",
    "ValueError: No available memory for the cache blocks",
    "error: unrecognized arguments: --max-batch 99",
)
_NODE_LOGS = (
    "HSA_STATUS_ERROR_OUT_OF_RESOURCES: hip device lost",
    "OSError: [Errno 5] Input/output error: '/shared/model/model.safetensors'",
    "hipErrorNoDevice: no ROCm-capable device is detected",
)
_OTHER_LOGS = ("", "Traceback (most recent call last):\nKeyError: 'x'", "waiting for the server")


@given(log=st.sampled_from(_CANDIDATE_LOGS), noise=st.text(max_size=40))
def test_a_server_that_died_of_the_candidates_own_cause_is_the_candidates(
    log: str, noise: str
) -> None:
    signal = signal_of(None, service_not_ready=True, service_log=f"{noise}\n{log}")
    for record in RecordState:
        assert classify(signal, record) is BenchmarkFailureKind.WORKLOAD


@given(log=st.sampled_from(_NODE_LOGS + _OTHER_LOGS), cause=st.sampled_from(_CANDIDATE_LOGS))
def test_a_server_that_never_became_ready_without_a_candidate_cause_is_ambiguous(
    log: str, cause: str
) -> None:
    for text in (log, f"{cause}\n{log}" if log in _NODE_LOGS else log):
        signal = signal_of(None, service_not_ready=True, service_log=text)
        for record in RecordState:
            assert classify(signal, record) is BenchmarkFailureKind.AMBIGUOUS


@given(kinds=st.lists(st.sampled_from((*_KINDS, None)), max_size=5))
def test_a_job_failure_follows_its_worst_stage(kinds: list[BenchmarkFailureKind | None]) -> None:
    failure = job_failure(kinds)
    if not kinds or BenchmarkFailureKind.INFRASTRUCTURE in kinds:
        assert failure is MeasurementFailure.INFRASTRUCTURE
    elif None in kinds:
        assert failure is MeasurementFailure.UNKNOWN
    elif BenchmarkFailureKind.AMBIGUOUS in kinds:
        assert failure is MeasurementFailure.AMBIGUOUS
    else:
        assert failure is MeasurementFailure.WORKLOAD


def _submissions(failure: MeasurementFailure, limit: int) -> int:
    """How many times a measurement that always ends with ``failure`` is submitted."""
    submitted = 1
    while may_resubmit(failure, submissions=submitted, limit=limit):
        submitted += 1
    return submitted


@given(failure=st.sampled_from(MeasurementFailure), limit=_BOUND)
def test_resubmission_follows_the_class(failure: MeasurementFailure, limit: int) -> None:
    submitted = _submissions(failure, limit)
    match failure:
        case MeasurementFailure.INFRASTRUCTURE:
            assert submitted == limit
        case MeasurementFailure.AMBIGUOUS:
            assert submitted == min(limit, 2)
        case _:
            # A candidate's failure, or one that proves nothing, is final at once.
            assert submitted == 1


@given(signal=_SIGNALS, record=_RECORDS, limit=_BOUND)
def test_a_candidate_failure_is_never_measured_again(
    signal: TerminalSignal, record: RecordState, limit: int
) -> None:
    kind = classify(signal, record)
    failure = job_failure([kind])
    if kind is BenchmarkFailureKind.WORKLOAD:
        assert not may_resubmit(failure, submissions=1, limit=limit)
    assert _submissions(failure, limit) <= limit
