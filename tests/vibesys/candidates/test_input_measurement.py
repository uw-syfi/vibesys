"""What the input's decoded readings say: a value for every outcome, decided by the evidence alone."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter

from vibesys.candidates.api import (
    Baseline,
    InputFailureKind,
    InputReading,
    Measured,
    NotConfigured,
    Unmeasurable,
    failure,
    input_from_readings,
    reading,
    trusted,
)
from vibesys.metrics import EvidenceReading
from vs_core.api import (
    EvidenceId,
    EvidenceKind,
    EvidenceRef,
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
        (kind for kind in (EvidenceKind.CORRECTNESS, EvidenceKind.BENCHMARK) if verdicts[kind] is False),
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


@settings(max_examples=examples(pr=8, nightly=100))
@given(accuracy_passed=st.none() | st.booleans())
def test_an_input_checkpointed_as_measured_with_failed_accuracy_reads_as_accuracy_failed(
    accuracy_passed: bool | None,
) -> None:
    """A checkpoint written before S173 kept a failed accuracy check as ``measured``."""
    written = Measured(reading=InputReading(accuracy_passed=accuracy_passed)).model_dump_json()

    found = TypeAdapter(Baseline).validate_json(written)

    why = failure(found)
    if accuracy_passed is False:
        assert why is not None
        assert why.kind is InputFailureKind.ACCURACY_FAILED
        assert not trusted(found)
    else:
        assert why is None
        assert trusted(found)


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
