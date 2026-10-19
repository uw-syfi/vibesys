"""What the input's decoded readings say: a value for every outcome, decided by the evidence alone."""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

from vibesys.candidates.api import (
    Baseline,
    InputFailure,
    InputFailureKind,
    InputReading,
    Measured,
    NotConfigured,
    Unmeasurable,
    again_after,
    failure,
    input_from_readings,
    input_metrics,
    input_value,
    reading,
    trusted,
    uncompared,
)
from vibesys.metrics import EvidenceReading, MetricRow
from vs_core.api import (
    EvidenceId,
    EvidenceKind,
    EvidenceRef,
    FailureReason,
    MeasurementFailure,
    ObservationStatus,
    RequestId,
    RevisionId,
    RevisionRef,
    RunId,
    Scope,
)
from vs_sim.api.testing import examples

_INPUT = RevisionRef(revision_id=RevisionId(root="input"), digest="d")
_KINDS = (EvidenceKind.CORRECTNESS, EvidenceKind.BENCHMARK)


def _ref(index: int, kind: EvidenceKind) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=EvidenceId(root=f"e{index}"),
        kind=kind,
        purpose="baseline",
        scope=Scope(owner=RunId(root="run"), generation=0),
        source_request=RequestId(root=f"request-{index}"),
        candidate=_INPUT,
        observation_sequence=1,
        evaluator_digest="e",
        workload_digest="w",
        environment_digest="n",
        provenance="trusted",
        status=ObservationStatus.SUCCEEDED,
    )


def _reading(
    index: int, kind: EvidenceKind, *, passed: bool, feedback: str = ""
) -> EvidenceReading:
    return EvidenceReading(
        evidence_id=EvidenceId(root=f"e{index}"),
        kind=kind,
        passed=passed,
        stage="s",
        feedback=feedback,
    )


# One verdict per measured kind, or None where the kind was not measured.
_VERDICTS = st.fixed_dictionaries({kind: st.none() | st.booleans() for kind in _KINDS}).filter(
    lambda verdicts: any(v is not None for v in verdicts.values())
)


def _asked(
    verdicts: dict[EvidenceKind, bool | None], feedback: str = ""
) -> tuple[tuple[EvidenceRef, ...], tuple[EvidenceReading, ...]]:
    kinds = [kind for kind in _KINDS if verdicts[kind] is not None]
    refs = tuple(_ref(i, kind) for i, kind in enumerate(kinds))
    readings = tuple(
        _reading(i, kind, passed=bool(verdicts[kind]), feedback=feedback)
        for i, kind in enumerate(kinds)
    )
    return refs, readings


@settings(max_examples=examples(pr=8, nightly=100))
@given(verdicts=_VERDICTS, feedback=st.text(max_size=20))
def test_the_input_is_measured_unless_a_gate_it_was_measured_by_failed(
    verdicts: dict[EvidenceKind, bool | None], feedback: str
) -> None:
    refs, readings = _asked(verdicts, feedback)

    found = input_from_readings(refs, "succeeded", readings)

    seen = reading(found)
    assert seen is not None
    assert seen.accuracy_passed == verdicts[EvidenceKind.CORRECTNESS]
    assert seen.benchmark_passed == verdicts[EvidenceKind.BENCHMARK]
    # Accuracy is judged first: a benchmark of an inaccurate input measures nothing to keep.
    failed = next(
        (
            kind
            for kind in (EvidenceKind.CORRECTNESS, EvidenceKind.BENCHMARK)
            if verdicts[kind] is False
        ),
        None,
    )
    if failed is None:
        assert isinstance(found, Measured)
        assert failure(found) is None
        assert trusted(found)
        return
    assert isinstance(found, Unmeasurable)
    assert found.failure.kind is (
        InputFailureKind.ACCURACY_FAILED
        if failed is EvidenceKind.CORRECTNESS
        else InputFailureKind.BENCHMARK_FAILED
    )
    assert found.failure.stage == "s"
    assert found.failure.output == feedback
    assert not trusted(found)


# Records as the version before S173 wrote them, byte for byte: a failed benchmark had no stage,
# and a failed accuracy check was a measured input that said so.
_PRE_S173_BENCHMARK_FAILED = (
    '{"kind":"unmeasurable","failure":{"kind":"benchmark-failed","status":null,'
    '"output":"crashed","ended":null,"cause":null},"reading":{"accuracy_passed":true,'
    '"benchmark_passed":false,"metrics":[],"partial":null}}'
)
_PRE_S173_ACCURACY_FAILED = (
    '{"kind":"measured","reading":{"accuracy_passed":false,"benchmark_passed":true,'
    '"metrics":[{"name":"throughput","value":100.0,"unit":"tok/s","direction":"max"}],'
    '"partial":null}}'
)


def test_a_failed_benchmark_checkpointed_before_s173_still_loads() -> None:
    """A checkpoint with ``benchmark-failed`` and no stage resumes on this build (F1)."""
    found = TypeAdapter(Baseline).validate_json(_PRE_S173_BENCHMARK_FAILED)

    assert isinstance(found, Unmeasurable)
    assert found.failure.kind is InputFailureKind.BENCHMARK_FAILED
    assert found.failure.stage == "benchmark"
    assert found.failure.output == "crashed"
    assert not trusted(found)


def test_a_failed_accuracy_check_checkpointed_before_s173_loads_as_the_failure_it_was() -> None:
    """The old ``measured`` shape reads as ``accuracy-failed`` everywhere, its bar kept (F4)."""
    found = TypeAdapter(Baseline).validate_json(_PRE_S173_ACCURACY_FAILED)

    assert isinstance(found, Unmeasurable)
    assert found.failure.kind is InputFailureKind.ACCURACY_FAILED
    assert found.failure.stage == "accuracy"
    assert not trusted(found)
    assert input_value(found) == 100.0  # its benchmark passed, so it stays the bar
    assert not uncompared(found)


@settings(max_examples=examples(pr=8, nightly=100))
@given(accuracy_passed=st.none() | st.booleans(), benchmark_passed=st.none() | st.booleans())
def test_every_measured_record_an_earlier_version_wrote_reads_as_one_shape(
    accuracy_passed: bool | None,  # noqa: FBT001
    benchmark_passed: bool | None,  # noqa: FBT001
) -> None:
    """Whatever a ``measured`` record said, the loaded input is measured only if it passed."""
    seen = {"accuracy_passed": accuracy_passed, "benchmark_passed": benchmark_passed}
    written = json.dumps({"kind": "measured", "reading": seen})

    found = TypeAdapter(Baseline).validate_json(written)

    assert isinstance(found, Measured) is (accuracy_passed is not False)
    assert trusted(found) is (accuracy_passed is not False)
    again = TypeAdapter(Baseline).validate_json(TypeAdapter(Baseline).dump_json(found))
    assert again == found  # the migrated shape is stable


def test_a_measured_input_that_failed_its_accuracy_check_cannot_be_made() -> None:
    with pytest.raises(ValidationError):
        Measured(reading=InputReading(accuracy_passed=False))


_ROWS = (MetricRow(name="throughput", value=100.0, unit="tok/s", direction="max"),)


@settings(max_examples=examples(pr=10, nightly=100))
@given(
    kind=st.sampled_from(list(InputFailureKind)),
    benchmark_passed=st.none() | st.booleans(),
    has_metrics=st.booleans(),
)
def test_a_failed_input_keeps_a_bar_only_when_just_its_accuracy_check_failed(
    kind: InputFailureKind,
    benchmark_passed: bool | None,  # noqa: FBT001
    *,
    has_metrics: bool,
) -> None:
    """F3(b): candidates must still beat a benchmark the input passed; otherwise nothing."""
    fields = {
        InputFailureKind.NO_EVIDENCE: {"status": ObservationStatus.FAILED},
        InputFailureKind.WAIT_ENDED: {"ended": FailureReason.TIMED_OUT},
    }.get(kind, {})
    seen = InputReading(
        accuracy_passed=kind is not InputFailureKind.ACCURACY_FAILED,
        benchmark_passed=benchmark_passed,
        metrics=_ROWS if has_metrics else (),
    )
    found = Unmeasurable(
        failure=InputFailure.model_validate({"kind": kind, **fields}), reading=seen
    )

    kept = kind is InputFailureKind.ACCURACY_FAILED and benchmark_passed is True and has_metrics
    assert input_metrics(found) == (_ROWS if kept else ())
    assert uncompared(found) is not kept
    assert input_metrics(Measured(reading=seen.model_copy(update={"accuracy_passed": True}))) == (
        seen.metrics
    )
    assert not uncompared(NotConfigured())
    assert not uncompared(None)


_ANY_FAILURE = st.one_of(
    st.builds(
        InputFailure,
        kind=st.just(InputFailureKind.NO_EVIDENCE),
        status=st.sampled_from(list(ObservationStatus)),
    ),
    st.builds(
        InputFailure,
        kind=st.just(InputFailureKind.WAIT_ENDED),
        ended=st.sampled_from(
            [item for item in FailureReason if item is not FailureReason.REFUSED]
        ),
    ),
    st.builds(
        InputFailure,
        kind=st.sampled_from(
            [InputFailureKind.READINGS_REFUSED, InputFailureKind.READINGS_UNDECODABLE]
        ),
    ),
    st.builds(
        InputFailure,
        kind=st.sampled_from([InputFailureKind.ACCURACY_FAILED, InputFailureKind.BENCHMARK_FAILED]),
        stage=st.just("s"),
        blamed=st.none() | st.sampled_from(list(MeasurementFailure)),
        output=st.sampled_from(["", "  \n", "assertion failed: 3 != 4"]),
    ),
)


@settings(max_examples=examples(pr=20, nightly=200))
@given(why=_ANY_FAILURE)
def test_the_input_is_measured_again_only_when_its_failure_shows_no_fault_of_it(
    why: InputFailure,
) -> None:
    """F3(a): no evidence, a wait that ran out or was lost, or a stage nobody blamed on it."""
    stage_failed = why.kind in {InputFailureKind.ACCURACY_FAILED, InputFailureKind.BENCHMARK_FAILED}
    unblamed = why.blamed not in {None, MeasurementFailure.WORKLOAD}
    expected = (
        why.kind is InputFailureKind.NO_EVIDENCE
        or (
            why.kind is InputFailureKind.WAIT_ENDED
            and why.ended in {FailureReason.TIMED_OUT, FailureReason.LOST, FailureReason.OWNER_GONE}
        )
        or (stage_failed and (unblamed or not why.output.strip()))
    )
    assert again_after(why) is expected


@settings(max_examples=examples(pr=8, nightly=100))
@given(verdicts=_VERDICTS, status=st.sampled_from(["failed", "cancelled", "unknown"]))
def test_a_measurement_that_did_not_succeed_leaves_the_input_undecoded(
    verdicts: dict[EvidenceKind, bool | None], status: str
) -> None:
    refs, readings = _asked(verdicts)

    found = input_from_readings(refs, status, readings)

    assert isinstance(found, Unmeasurable)
    assert found.failure.kind is InputFailureKind.READINGS_UNDECODABLE
    assert not trusted(found)


@settings(max_examples=examples(pr=8, nightly=100))
@given(verdicts=_VERDICTS)
def test_readings_that_name_a_record_nobody_asked_about_are_refused(
    verdicts: dict[EvidenceKind, bool | None],
) -> None:
    refs, readings = _asked(verdicts)
    stray = (*readings, _reading(len(refs), EvidenceKind.BENCHMARK, passed=True))

    found = input_from_readings(refs, "succeeded", stray)

    assert isinstance(found, Unmeasurable)
    assert found.failure.kind is InputFailureKind.READINGS_REFUSED
    assert reading(found) is None


def test_no_readings_at_all_leave_the_input_undecoded() -> None:
    found = input_from_readings((), "succeeded", ())

    assert isinstance(found, Unmeasurable)
    assert found.failure.kind is InputFailureKind.READINGS_UNDECODABLE


def test_a_task_with_no_gate_has_nothing_to_measure_and_nothing_to_distrust() -> None:
    assert trusted(NotConfigured())
    assert reading(NotConfigured()) is None
    assert failure(NotConfigured()) is None
