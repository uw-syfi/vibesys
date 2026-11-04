"""Whose fault a failed benchmark is: one table, total, and never lossy."""

from __future__ import annotations

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import ValidationError

from vs_core.api import MeasurementFailure
from vs_runtime.api import BenchmarkFailureKind, EvaluationRetries
from vs_runtime.api.infrastructure import (
    RecordState,
    TerminalSignal,
    TrustedEvaluationPlan,
    classify,
    job_failure,
    signal_of,
)

_KINDS = (
    BenchmarkFailureKind.WORKLOAD,
    BenchmarkFailureKind.INFRASTRUCTURE,
    BenchmarkFailureKind.AMBIGUOUS,
)
_BOUND = st.integers(min_value=1, max_value=5)
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


# Facts true of any process: the library knows them with no task's help.
_GENERIC_CANDIDATE_LOGS = (
    "torch.OutOfMemoryError: CUDA out of memory",
    "HIP error: out of memory",
    "OSError: [Errno 12] Cannot allocate memory",
    "error: unrecognized arguments: --max-batch 99",
    "/cluster/work/venv/bin/python: Error while finding module specification for "
    "'engine.server' (ModuleNotFoundError: No module named 'engine')",
    "ModuleNotFoundError: No module named 'triton_kernels'",
)
# What one task's server prints: only that task's domain can say so.
_TASK_LOGS = (
    "ValueError: No available memory for the cache blocks",
    "ValueError: prompt is larger than the maximum number of tokens that can be stored in kv cache",
)
_TASK_CAUSES = (
    r"no available memory for the cache blocks",
    r"larger than the maximum number of tokens that can be stored in kv cache",
)
# One line per node-fault signature the classifier knows, in the case a log may print it.
_NODE_LOGS = (
    "HSA_STATUS_ERROR_OUT_OF_RESOURCES: hip device lost",
    "OSError: [Errno 5] Input/output error: '/shared/model/model.safetensors'",
    "hipErrorNoDevice: no ROCm-capable device is detected",
    "RuntimeError: No GPU devices found",
    "OSError: [Errno 28] No space left on device",
    "OSError: [Errno 116] Stale file handle",
    "STALE FILE HANDLE",
    "OSError: [Errno 107] Transport endpoint is not connected",
)
# Filler that no cause or node-fault signature can be spelled from (they all need letters),
# so it never changes what a log says.
_NOISE = st.text(alphabet="0123456789 .,:;-_/[]()", max_size=40)
_OTHER_LOGS = ("", "Traceback (most recent call last):\nKeyError: 'x'", "waiting for the server")
_CAUSES = st.lists(st.sampled_from(_TASK_CAUSES), unique=True).map(tuple)


def _startup_kind(log: str, causes: tuple[str, ...]) -> set[BenchmarkFailureKind]:
    """The class of a never-ready server's failure under every record state."""
    signal = signal_of(
        None, service_not_ready=True, service_log=log, candidate_startup_causes=causes
    )
    return {classify(signal, record) for record in RecordState}


@given(log=st.sampled_from(_GENERIC_CANDIDATE_LOGS), noise=_NOISE, causes=_CAUSES)
@example(log=_GENERIC_CANDIDATE_LOGS[4], noise="", causes=())
def test_a_server_that_died_of_a_fact_true_of_any_process_is_the_candidates_whatever_the_task(
    log: str, noise: str, causes: tuple[str, ...]
) -> None:
    assert _startup_kind(f"{noise}\n{log}", causes) == {BenchmarkFailureKind.WORKLOAD}


@given(log=st.sampled_from(_TASK_LOGS), noise=_NOISE)
def test_a_task_cause_is_the_candidates_only_when_the_task_names_it(log: str, noise: str) -> None:
    text = f"{noise}\n{log}"
    assert _startup_kind(text, ()) == {BenchmarkFailureKind.AMBIGUOUS}
    assert _startup_kind(text, _TASK_CAUSES) == {BenchmarkFailureKind.WORKLOAD}


@given(
    log=st.text(max_size=120),
    causes=_CAUSES,
    exit_code=st.none() | st.integers(min_value=-300, max_value=300),
    ready=st.booleans(),
)
def test_a_task_cause_only_adds_to_what_the_generic_facts_say(
    log: str, causes: tuple[str, ...], exit_code: int | None, *, ready: bool
) -> None:
    bare = signal_of(exit_code, service_not_ready=not ready, service_log=log)
    with_task = signal_of(
        exit_code, service_not_ready=not ready, service_log=log, candidate_startup_causes=causes
    )
    if bare is TerminalSignal.SERVICE_CANDIDATE_FAULT:
        assert with_task is bare
    elif with_task is not bare:
        assert (bare, with_task) == (
            TerminalSignal.SERVICE_NOT_READY,
            TerminalSignal.SERVICE_CANDIDATE_FAULT,
        )


@given(
    log=st.sampled_from(_NODE_LOGS + _OTHER_LOGS),
    cause=st.sampled_from(_GENERIC_CANDIDATE_LOGS + _TASK_LOGS),
    causes=_CAUSES,
)
@example(log="STALE FILE HANDLE", cause=_TASK_LOGS[0], causes=_TASK_CAUSES)
def test_a_server_that_never_became_ready_without_a_candidate_cause_is_ambiguous(
    log: str, cause: str, causes: tuple[str, ...]
) -> None:
    for text in (log, f"{cause}\n{log}" if log in _NODE_LOGS else log):
        assert _startup_kind(text, causes) == {BenchmarkFailureKind.AMBIGUOUS}


@given(causes=st.lists(st.text(max_size=8)))
def test_a_plan_rejects_a_cause_that_is_not_a_regular_expression(causes: list[str]) -> None:
    bad = [*causes, "("]
    with pytest.raises(ValidationError, match="not a regular expression"):
        TrustedEvaluationPlan(candidate_startup_causes=tuple(bad))


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


def _retries(limit: int) -> EvaluationRetries:
    return EvaluationRetries(infrastructure_attempts=limit, ambiguous_attempts=min(limit, 2))


def _submissions(failure: MeasurementFailure, limit: int) -> int:
    """How many times a backend measures a candidate whose every try ends with ``failure``."""
    retries = _retries(limit)
    submitted = 1
    while retries.allows_another(failure, tries=submitted):
        submitted += 1
    return submitted


@given(failure=st.sampled_from(MeasurementFailure), limit=_BOUND)
def test_the_backend_measures_again_by_the_class_of_the_failure(
    failure: MeasurementFailure, limit: int
) -> None:
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
        assert not _retries(limit).allows_another(failure, tries=1)
    assert _submissions(failure, limit) <= limit
