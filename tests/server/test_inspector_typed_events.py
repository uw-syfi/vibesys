"""The inspector's fallback answers follow typed events and never the run log text."""

from datetime import UTC, datetime
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st
from tests.server.support import build_server_parts

from server.events import (
    AgentExecutionFinishedData,
    BenchmarkResultData,
    EventData,
    EventStatus,
    EventType,
    GateFinishedData,
    GateKind,
    JudgeResultData,
    RoundFinishedData,
    RunEvent,
)
from server.read_model import RunInspector

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_MISLEADING = "judge verdict: pass round 9 benchmark latency 0.1 ms failed"


class FakeInspectionSource:
    """A run integration over a fixed event history, recording the queries it is asked."""

    def __init__(self, events: list[RunEvent], current_round: str | None = None) -> None:
        self.events = events
        self.current_round = current_round
        self.queries: list[str] = []

    def status(self) -> str:
        return "running"

    def record(
        self,
        event_type: EventType,
        text: str = "",
        *,
        data: EventData | None = None,
        **fields: object,
    ) -> RunEvent:
        assert event_type is EventType.STATUS_QUERY
        assert data is None
        assert not fields
        self.queries.append(text)
        return RunEvent(timestamp=_T0, type=event_type, text=text)

    def read_history_events(self) -> list[RunEvent]:
        return list(self.events)


def _event(
    event_type: EventType,
    data: EventData,
    *,
    status: EventStatus | None = None,
    round_label: str | None = None,
) -> RunEvent:
    return RunEvent(
        timestamp=_T0, type=event_type, data=data, status=status, round_label=round_label
    )


def _judge(marker: str, *, fail: bool = False, round_label: str | None = None) -> RunEvent:
    return _event(
        EventType.JUDGE_RESULT,
        JudgeResultData(verdict="fail" if fail else "pass", feedback=f"fb-{marker}", attempt=1),
        round_label=round_label,
    )


def _benchmark(marker: str) -> RunEvent:
    return _event(
        EventType.BENCHMARK_RESULT,
        BenchmarkResultData(metric=f"metric-{marker}", value=1.5, unit="ms"),
    )


def _failed_execution(marker: str, round_label: str | None = None) -> RunEvent:
    return _event(
        EventType.AGENT_EXECUTION_FINISHED,
        AgentExecutionFinishedData(error=f"err-{marker}"),
        status=EventStatus.FAILED,
        round_label=round_label,
    )


def _failed_gate(marker: str) -> RunEvent:
    return _event(
        EventType.GATE_FINISHED,
        GateFinishedData(gate=GateKind.VALIDATION, recipe="unit", output_tail=f"tail-{marker}"),
        status=EventStatus.FAILED,
    )


def _round(number: int) -> RunEvent:
    return _event(
        EventType.ROUND_FINISHED,
        RoundFinishedData(attempts=2, judge_verdict="pass", profile_skipped=True),
        status=EventStatus.COMPLETED,
        round_label=f"round-{number}",
    )


_MARKER_COUNT = st.integers(min_value=1, max_value=6)


@given(count=_MARKER_COUNT)
def test_latest_judge_result_wins(count: int) -> None:
    source = FakeInspectionSource([_judge(f"j{i}") for i in range(count)])

    answer = RunInspector(source).answer("what did the judge say?")

    assert f"fb-j{count - 1}" in answer
    assert all(f"fb-j{i}" not in answer for i in range(count - 1))
    assert source.queries == ["what did the judge say?"]


@given(count=_MARKER_COUNT, passing_gate_last=st.booleans())
def test_latest_benchmark_result_wins(*, count: int, passing_gate_last: bool) -> None:
    events = [_benchmark(f"b{i}") for i in range(count)]
    if passing_gate_last:
        events.append(
            _event(
                EventType.GATE_FINISHED,
                GateFinishedData(
                    gate=GateKind.BENCHMARK, metric="gate-metric", value=2.0, unit="s"
                ),
                status=EventStatus.COMPLETED,
            )
        )

    answer = RunInspector(FakeInspectionSource(events)).answer("show the benchmark")

    if passing_gate_last:
        assert "gate-metric" in answer
        assert "metric-b" not in answer
    else:
        assert f"metric-b{count - 1}" in answer
        assert all(f"metric-b{i}" not in answer for i in range(count - 1))


@given(count=_MARKER_COUNT, gate_last=st.booleans())
def test_latest_failure_is_the_newest_failed_execution_or_gate(
    *, count: int, gate_last: bool
) -> None:
    events = [_failed_execution(f"e{i}") for i in range(count)]
    events.append(_failed_gate("g") if gate_last else _failed_execution("last"))
    events.append(_judge("later", fail=True))

    answer = RunInspector(FakeInspectionSource(events)).answer("why did it fail?")

    assert ("tail-g" if gate_last else "err-last") in answer
    assert all(f"err-e{i}" not in answer for i in range(count))
    assert "fb-later" not in answer


@given(
    rounds=st.lists(st.integers(min_value=1, max_value=8), min_size=1, max_size=6, unique=True),
    asked=st.integers(min_value=1, max_value=8),
)
def test_round_detail_lists_only_that_rounds_events(rounds: list[int], asked: int) -> None:
    events = [
        event
        for number in rounds
        for event in (
            _judge(f"r{number}", round_label=f"round-{number}"),
            _failed_execution(f"r{number}", round_label=f"round-{number}"),
            _round(number),
        )
    ]

    answer = RunInspector(FakeInspectionSource(events)).answer(f"tell me about round {asked}")

    if asked in rounds:
        assert f"fb-r{asked}" in answer
        assert f"err-r{asked}" in answer
        assert all(f"fb-r{n}" not in answer for n in rounds if n != asked)
    else:
        assert answer == f"No recorded events found for round {asked}."


@given(finished=st.lists(st.integers(min_value=1, max_value=8), min_size=1, max_size=5))
def test_previous_round_without_a_current_round_is_the_latest_finished(
    finished: list[int],
) -> None:
    events = [
        event for n in finished for event in (_judge(f"r{n}", round_label=f"round-{n}"), _round(n))
    ]

    answer = RunInspector(FakeInspectionSource(events)).answer("what happened last round?")

    assert f"fb-r{max(finished)}" in answer


@given(current=st.integers(min_value=1, max_value=8))
def test_previous_round_with_a_current_round_is_the_one_before(current: int) -> None:
    events = [_judge(f"r{n}", round_label=f"round-{n}") for n in range(1, current + 1)]
    source = FakeInspectionSource(events, current_round=f"round {current}")

    answer = RunInspector(source).answer("and the previous round?")

    if current == 1:
        assert answer == "No previous round has been recorded yet."
        assert "fb-r1" not in answer
    else:
        assert f"fb-r{current - 1}" in answer
        assert f"fb-r{current}" not in answer
    assert source.queries == ["and the previous round?"]


def test_missing_events_say_so_and_unmatched_questions_explain_the_matcher() -> None:
    inspector = RunInspector(FakeInspectionSource([]))

    assert inspector.answer("why did it fail?") == (
        "No failed agent execution or gate has been recorded yet."
    )
    assert inspector.answer("judge?") == "No judge result has been recorded yet."
    assert inspector.answer("benchmark?") == "No benchmark result has been recorded yet."
    assert "matches on keywords" in inspector.answer("tell me a joke")


def test_answers_follow_events_not_a_misleading_run_log(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    (tmp_path / "run-1.log").write_text(_MISLEADING + "\n", encoding="utf-8")
    parts.integration.record(
        EventType.JUDGE_RESULT,
        data=JudgeResultData(verdict="fail", feedback="typed-feedback", attempt=1),
        round_label="round-2",
    )
    inspector = RunInspector(parts.integration)

    for question in ("judge?", "benchmark?", "round 9", "why did it fail?"):
        answer = inspector.answer(question)
        assert "pass round 9" not in answer
        assert "latency 0.1" not in answer
    assert "typed-feedback" in inspector.answer("judge?")
    assert inspector.answer("round 9") == "No recorded events found for round 9."
