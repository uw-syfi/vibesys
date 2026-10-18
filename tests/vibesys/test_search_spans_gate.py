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
            names = draw(
                st.lists(
                    st.sampled_from(("throughput", "latency", "memory")), unique=True, min_size=1
                )
            )
            metrics = tuple(
                MetricRow(
                    name=name,
                    value=draw(st.floats(0.1, 1e6)),
                    direction="min" if name != "throughput" else "max",
                    unit=draw(st.sampled_from((None, "ms"))),
                )
                for name in names
            )
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
@given(
    readings=_readings(),
    input_values=st.lists(st.floats(0.1, 1e6), max_size=2),
)
def test_a_gate_reads_the_benchmark_headline_and_the_first_check_that_rejected(
    readings: list[EvidenceReading], input_values: list[float]
) -> None:
    input_metrics = tuple(
        MetricRow(name=name, value=value, direction="max")
        for name, value in zip(("throughput", "latency"), input_values, strict=False)
    )
    input_value = input_metrics[0].value if input_metrics else None
    attrs = GateAttrs.read("accuracy,benchmark", readings, input_metrics)
    # Where the input stands on every axis reaches the label too, headline first.
    assert attrs.input_metrics == input_metrics

    benchmark = _latest(readings, EvidenceKind.BENCHMARK)
    value = None if benchmark is None or not benchmark.metrics else benchmark.metrics[0].value
    assert attrs.metric_value == value
    # Every reading the benchmark reported reaches the label, headline first, units and all.
    assert attrs.metrics == (() if benchmark is None else benchmark.metrics)
    assert GateAttrs.model_validate_json(attrs.model_dump_json()).metrics == attrs.metrics
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


def test_a_benchmark_that_reports_two_objectives_labels_its_gate_with_both() -> None:
    reading = EvidenceReading(
        evidence_id=EvidenceId(root="benchmark"),
        kind=EvidenceKind.BENCHMARK,
        passed=True,
        stage="benchmark",
        metrics=(
            MetricRow(name="throughput", value=120.0, direction="max", unit="ops/s"),
            MetricRow(name="p99_latency", value=8.5, direction="min", unit="ms"),
        ),
    )

    input_rows = (
        MetricRow(name="throughput", value=100.0, direction="max", unit="ops/s"),
        MetricRow(name="p99_latency", value=10.0, direction="min", unit="ms"),
    )
    attrs = GateAttrs.read("benchmark", [reading], input_rows)

    assert attrs.metric_value == 120.0
    assert attrs.vs_baseline == 1.2
    assert [(m.name, m.value) for m in attrs.input_metrics] == [
        ("throughput", 100.0),
        ("p99_latency", 10.0),
    ]
    assert [(m.name, m.value, m.unit, m.direction) for m in attrs.metrics] == [
        ("throughput", 120.0, "ops/s", "max"),
        ("p99_latency", 8.5, "ms", "min"),
    ]
