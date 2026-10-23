"""The input's measurement is one typed fact, written once where records are made."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vibesys.events as e
import vs_proto.api as p
from vibesys.records._common import InputReboundError, MetricReboundError
from vibesys.records.api import MappingState, map_event
from vs_runlog.api import FakeFileSync, FileRunLogWriter, read_run_head
from vs_sim.api.testing import ManualClock, SimThreads

if TYPE_CHECKING:
    import random

_AT = datetime(2026, 1, 1, tzinfo=UTC)
_SERIES = "speed"


def _event(kind: e.CoreEventType, data: e.CoreEventData | None = None) -> e.CoreEvent:
    return e.CoreEvent.model_validate(
        {"type": kind, "data": data, "timestamp": _AT, "run_id": "run-a", "sequence": 1}
    )


def _gate(span_id: str, parent: str | None, value: float | None) -> e.CoreEvent:
    rows = [] if value is None else [{"name": _SERIES, "value": value, "direction": "max"}]
    span = e.SpanStartedData.model_validate(
        {
            "span": {
                "id": span_id,
                "parent": parent,
                "type": "operation",
                "name": "gate",
                "status": "done",
                "started_at": 0,
                "ended_at": 1,
                "label": {
                    "kind": "gate",
                    "attrs_json": json.dumps({"stage": "benchmark", "metrics": rows}),
                },
            }
        }
    ).span
    return _event(e.CoreEventType.SPAN_ENDED, e.SpanEndedData(span=span))


def _workstream(span_id: str) -> e.CoreEvent:
    span = e.SpanStartedData.model_validate(
        {
            "span": {
                "id": span_id,
                "parent": None,
                "type": "operation",
                "name": "workstream",
                "status": "running",
                "started_at": 0,
                "label": {"kind": "workstream", "attrs_json": "{}"},
            }
        }
    ).span
    return _event(e.CoreEventType.SPAN_STARTED, e.SpanStartedData(span=span))


def _started() -> e.CoreEvent:
    overview = e.RunOverview(objective=e.ObjectiveOverview(text="t", metric=_SERIES))
    return _event(
        e.CoreEventType.RUN_STARTED,
        e.RunStartedData(outer_loop="single-agent", input="input", overview=overview),
    )


def _records(events: list[e.CoreEvent]) -> list[p.Record]:
    state = MappingState("run-a")
    records: list[p.Record] = []
    for event in events:
        mapped = map_event(state, event)
        records.extend(mapped.records)
        state = mapped.state
    return records


def _fact(records: list[p.Record]) -> p.InputMeasurement | None:
    epoch = None
    for record in records:
        if record.body is not None and isinstance(record.body.value, p.EpochStarted):
            epoch = record.body.value
    return None if epoch is None or epoch.run is None else epoch.run.input_measurement


@st.composite
def gate_sets(draw: st.DrawFn) -> tuple[list[float | None], list[float]]:
    """Input gates (some measured, some not) and the candidates' readings."""
    inputs = draw(st.lists(st.one_of(st.none(), st.floats(1, 100)), min_size=1, max_size=4))
    candidates = draw(st.lists(st.floats(1, 100), max_size=3))
    return inputs, candidates


@given(gates=gate_sets(), shuffle=st.randoms(use_true_random=False))
def test_the_input_fact_is_the_input_gates_measurement_in_any_order(
    gates: tuple[list[float | None], list[float]], shuffle: random.Random
) -> None:
    inputs, candidates = gates
    measured = [v for v in inputs if v is not None]
    # One input measurement per run; the other input gates carry none (build, validation).
    inputs = [measured[0] if i == 0 else None for i, v in enumerate(inputs)] if measured else inputs
    events = [_gate(f"op:input-{i}", None, v) for i, v in enumerate(inputs)]
    events += [_gate(f"op:cand-{i}", "op:ws", v) for i, v in enumerate(candidates)]
    # Any interleaving of the gates; a workstream is open before its own gates, as it is in a run.
    shuffle.shuffle(events)
    records = _records([_started(), _workstream("op:ws"), *events])
    fact = _fact(records)
    first = next((v for v in inputs if v is not None), None)
    if first is None:
        assert fact is None
        return
    assert fact is not None
    assert fact.state is not None
    assert fact.state.field == "measured"
    assert fact.state.value.value == first
    p.validate(fact)
    # The head and a proto-era reader (a run without the field) state the same value.
    for stated in (records, _without_fact(records)):
        head = _head(stated)
        assert head.objective is not None
        assert head.objective.input_value == first


def _head(records: list[p.Record]) -> p.RunHead:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "run.log"
        with FileRunLogWriter(
            path, clock=ManualClock(), threads=SimThreads(), sync=FakeFileSync()
        ) as writer:
            for record in records:
                writer.append(record)
            writer.checkpoint()
        return read_run_head(path)


def _without_fact(records: list[p.Record]) -> list[p.Record]:
    kept = []
    for record in records:
        copy = p.Record.from_binary(record.to_binary())
        if copy.body is not None and isinstance(copy.body.value, p.EpochStarted):
            run = copy.body.value.run
            if run is not None:
                run.clear_field("input_measurement")
        kept.append(copy)
    return kept


def test_a_second_different_input_measurement_is_a_typed_error() -> None:
    with pytest.raises(InputReboundError):
        _records([_started(), _gate("op:input-0", None, 5.0), _gate("op:input-1", None, 6.0)])
    # The same value again is nothing.
    assert _fact(_records([_started(), _gate("op:i0", None, 5.0), _gate("op:i1", None, 5.0)]))


def test_a_reading_at_an_identity_cannot_be_rebound() -> None:
    with pytest.raises(MetricReboundError):
        _records([_started(), _gate("op:input-0", None, 5.0), _gate("op:input-0", None, 7.0)])


def test_a_failed_input_is_an_explicit_unmeasured_state() -> None:
    failed = _event(
        e.CoreEventType.INPUT_MEASUREMENT_FAILED,
        e.InputMeasurementFailedData(
            failure="benchmark-failed", attempts=1, compared=0, summary="x"
        ),
    )
    fact = _fact(_records([_started(), failed]))
    assert fact is not None
    assert fact.state is not None
    assert fact.state.field == "unmeasured"


def _spanless_gate(execution_id: str | None, value: float | None = None) -> e.CoreEvent:
    """A gate event of an older journal: no span, and an execution id (or none)."""
    return e.CoreEvent.model_validate(
        {
            "type": e.CoreEventType.GATE_FINISHED,
            "data": e.GateFinishedData(
                gate=e.GateKind.BENCHMARK,
                metric=None if value is None else _SERIES,
                value=value,
            ),
            "timestamp": _AT,
            "run_id": "run-a",
            "sequence": 1,
            "execution_id": execution_id,
        }
    )


def _agent_started() -> e.CoreEvent:
    return _event(
        e.CoreEventType.AGENT_EXECUTION_STARTED,
        e.AgentExecutionStartedData(
            stage="implementer",
            activity=e.AgentExecutionActivityData(mode="thinking", summary="working"),
        ),
    ).model_copy(update={"execution_id": "turn-a"})


def _gate_subjects(records: list[p.Record]) -> list[p.GateSubject]:
    subjects = []
    for record in records:
        body = record.body
        if body is None or body.field != "span":
            continue
        label = body.value.span.label if body.value.span is not None else None
        if label is not None and label.kind == "gate" and label.attrs is not None:
            subjects.append(p.GateAttrs.from_binary(label.attrs.value).subject)
    return subjects


@given(executions=st.lists(st.text("abc", min_size=1, max_size=3), min_size=1, max_size=3))
def test_a_spanless_gate_is_the_inputs_until_a_candidate_has_started(
    executions: list[str],
) -> None:
    before = _records([_started(), *[_spanless_gate(name) for name in executions]])
    assert _gate_subjects(before) == [p.GateSubject.INPUT] * len(executions)
    after = _records(
        [_started(), _workstream("op:ws"), *[_spanless_gate(name) for name in executions]]
    )
    assert _gate_subjects(after) == [p.GateSubject.CANDIDATE] * len(executions)


@given(
    first=st.floats(1, 100),
    later=st.lists(st.floats(1, 100), min_size=1, max_size=3),
)
def test_a_spanless_gate_after_an_agent_ran_is_a_candidates_not_a_second_input(
    first: float, later: list[float]
) -> None:
    """Older multi-agent journals evaluate a candidate before any workstream span exists.

    The input is measured before any agent runs, and its measurement stays the first reading. A
    later spanless gate that carries an execution is a candidate's, whatever its value.
    """
    events = [
        _started(),
        _spanless_gate(None, first),
        _spanless_gate("input-run", first),
        _agent_started(),
        *[_spanless_gate(f"vs-{i}", value) for i, value in enumerate(later)],
    ]
    records = _records(events)
    fact = _fact(records)
    assert fact is not None
    assert fact.state is not None
    assert fact.state.field == "measured"
    assert fact.state.value.value == first
    subjects = _gate_subjects(records)
    assert subjects[-1] is p.GateSubject.CANDIDATE
    assert subjects[0] is p.GateSubject.INPUT


def test_a_spanless_gate_before_the_input_was_measured_stays_the_inputs_after_an_agent() -> None:
    records = _records([_started(), _agent_started(), _spanless_gate("run", 5.0)])
    assert _gate_subjects(records) == [p.GateSubject.INPUT]
    fact = _fact(records)
    assert fact is not None
    assert fact.state is not None
    assert fact.state.field == "measured"
