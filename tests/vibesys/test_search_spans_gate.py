"""A gate's label says what its readings measured and which trusted check rejected the candidate.

Every search labels its gates from the readings through ``GateAttrs.read``, so a client reads the
same value, ratio and failed check whatever the search: the value is the benchmark's headline,
the ratio is that over the input's, and ``failed`` names the first check (accuracy, then
benchmark) that rejected, with the progress its evaluator reported ("93 of 100 trials").
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.metrics import EvidenceReading, MetricRow, PartialRow
from vibesys.search_spans.api import FailedCheck, GateAttrs
from vs_core.api import EvidenceId, EvidenceKind
from vs_sim.api.testing import examples

_KINDS = (EvidenceKind.CORRECTNESS, EvidenceKind.BENCHMARK, EvidenceKind.PROFILING)


@st.composite
def _readings(draw: st.DrawFn) -> list[EvidenceReading]:
    found = []
    for number in range(draw(st.integers(0, 4))):
        kind = draw(st.sampled_from(_KINDS))
        passed = draw(st.booleans())
        metrics = ()
        if draw(st.booleans()):
            value = draw(st.floats(0.1, 1e6))
            metrics = (MetricRow(name="throughput", value=value, direction="max"),)
        partial = None
        if not passed and draw(st.booleans()):
            required = draw(st.integers(1, 1000))
            partial = PartialRow(
                name="passes",
                value=0.0,
                direction="max",
                completed=draw(st.integers(0, required)),
                required=required,
                progress_unit="trials",
            )
        found.append(
            EvidenceReading(
                evidence_id=EvidenceId(root=f"e{number}"),
                kind=kind,
                passed=passed,
                stage="s",
                metrics=metrics,
                partial=partial,
            )
        )
    return found


def _latest(readings: list[EvidenceReading], kind: EvidenceKind) -> EvidenceReading | None:
    matching = [r for r in readings if r.kind is kind]
    return matching[-1] if matching else None


@settings(max_examples=examples(pr=200, nightly=2000))
@given(readings=_readings(), input_value=st.one_of(st.none(), st.floats(0.1, 1e6)))
def test_a_gate_reads_the_benchmark_headline_and_the_first_check_that_rejected(
    readings: list[EvidenceReading], input_value: float | None
) -> None:
    attrs = GateAttrs.read("accuracy,benchmark", readings, input_value)

    benchmark = _latest(readings, EvidenceKind.BENCHMARK)
    value = None if benchmark is None or not benchmark.metrics else benchmark.metrics[0].value
    assert attrs.metric_value == value
    if value is None or input_value is None:
        assert attrs.vs_baseline is None
    else:
        assert attrs.vs_baseline == value / input_value

    rejected = [
        r
        for r in (_latest(readings, k) for k in (EvidenceKind.CORRECTNESS, EvidenceKind.BENCHMARK))
        if r is not None and not r.passed
    ]
    if not rejected:
        assert attrs.failed is None, "every check it read passed"
        return
    first = rejected[0]
    assert attrs.failed is not None
    assert attrs.failed.check is first.kind
    partial = first.partial
    assert (attrs.failed.completed, attrs.failed.required, attrs.failed.unit) == (
        (None, None, None)
        if partial is None
        else (partial.completed, partial.required, partial.progress_unit)
    )


def test_a_correctness_check_that_passed_93_of_100_trials_says_so() -> None:
    reading = EvidenceReading(
        evidence_id=EvidenceId(root="accuracy"),
        kind=EvidenceKind.CORRECTNESS,
        passed=False,
        stage="accuracy",
        partial=PartialRow(
            name="passes",
            value=93.0,
            direction="max",
            completed=93,
            required=100,
            progress_unit="trials",
        ),
    )

    assert FailedCheck.of([reading]) == FailedCheck(
        check=EvidenceKind.CORRECTNESS, completed=93, required=100, unit="trials"
    )
