"""A run tile's data: the digest's per-idea points and the attention reason.

The points are the run page's chart (``performanceFold`` in ``clients/features/run-page``). The two
folds are held to one rule by scenarios both sides read (``fixtures/run-dashboard``): each says what each idea
did (its outcome, its reading and when it landed), a journal is built from it, and the digest's
points must equal the chart the scenario's plain statement gives. The page's own test
(``run-dashboard-conformance.test.ts``) builds the same journals from the same shape of scenario
and holds its fold to the same statement, so the two cannot drift apart without a test failing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import BaseModel
from tests.server.host_helper_support import RUN_ID, fake_host, project_with_run

from server.host_api import (
    HELPER_LINE_MAX_BYTES,
    RUN_EVENTS_FILE,
    FailureReason,
    RunClosing,
    RunDigest,
    RunEndingFact,
    RunEndOutcome,
    RunPhase,
    RunsListReply,
    RunsListRequest,
    RunStatus,
    request_features,
)
from server.host_helper_requests import answer
from server.host_run_dashboard import (
    DASHBOARD_MAX_POINTS,
    REPEATED_FAILURE_AFTER,
    STALLED_AFTER_S,
    BestReading,
    IdeaVerdict,
    RunAttention,
    RunDashboard,
)
from server.run_digest import digest_journal, run_attention
from vs_project.api import Project
from vs_sim.api.testing import SimNetwork, SimThreads

if TYPE_CHECKING:
    from pydantic import JsonValue

_INPUT = 100.0
_BASE = 1_790_000_000
"""The journal time, in epoch seconds, of a scenario's time zero."""
"""The input's reading in every scenario: an idea's ratio is its reading over this."""


@dataclass(frozen=True)
class _Idea:
    """One idea of a scenario: how it ended, what its gate read, and when the reading landed."""

    outcome: str  # passed | failed | running
    reading: float | None
    landed: float


def _event(sequence: int, kind: str, at: float, data: dict[str, JsonValue]) -> bytes:
    document = {
        "protocol_version": 1,
        "sequence": sequence,
        "run_id": "20261010-120000-11111111-dynamic",
        "timestamp": datetime.fromtimestamp(_BASE + at, UTC).isoformat(),
        "type": kind,
        "text": "",
        "data": {"kind": kind, **data},
    }
    return json.dumps(document).encode() + b"\n"


@dataclass(frozen=True)
class _State:
    """Where a span stands: its status, its times (run-clock seconds) and its label's verdict."""

    status: str
    started: float
    ended: float | None
    verdict: str | None


def _span(
    kind: str, span_id: str, parent: str | None, state: _State, attrs: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    return {
        "id": span_id,
        "parent": parent,
        "status": state.status,
        "started_at": state.started,
        "ended_at": state.ended,
        "label": {"kind": kind, "verdict": state.verdict, "attrs_json": json.dumps(attrs)},
    }


def journal_of(
    ideas: list[_Idea],
    direction: str = "max",
    metric: str | None = None,
    unit: str | None = None,
    *,
    input_measured: bool = True,
) -> list[bytes]:
    """The journal of a run that played ``ideas``, one workstream and one gate each, in order."""
    objective: dict[str, JsonValue] = {"direction": direction}
    if metric is not None:
        objective["metric"] = metric
    if unit is not None:
        objective["unit"] = unit
    lines = [_event(1, "run_started", 0, {"overview": {"objective": objective}})]

    def add(kind: str, at: float, span: dict[str, JsonValue]) -> None:
        lines.append(_event(len(lines) + 1, kind, at, {"span": span}))

    for index, idea in enumerate(ideas):
        began = 10.0 * index
        ws = f"flow:{index}"
        attrs: dict[str, JsonValue] = {"hypothesis_id": f"h{index}", "probe": False}
        started = _State("running", began, None, None)
        add("span_started", began, _span("workstream", ws, None, started, attrs))
        if idea.reading is not None:
            gate_attrs: dict[str, JsonValue] = {"metric_value": idea.reading}
            if input_measured:
                gate_attrs["vs_baseline"] = idea.reading / _INPUT
            verdict = "passed" if idea.outcome == "passed" else "failed_check"
            gate = _State("done", idea.landed - 0.5, idea.landed, verdict)
            add("span_ended", idea.landed, _span("gate", f"op:{index}", ws, gate, gate_attrs))
        if idea.outcome != "running":
            status = "done" if idea.outcome == "passed" else "failed"
            done = _State(status, began, idea.landed + 1, idea.outcome)
            add("span_ended", idea.landed + 1, _span("workstream", ws, None, done, attrs))
    return lines


def _landed(ideas: list[_Idea], at: int) -> float:
    """Where idea ``at`` is plotted: when its reading landed, else when it last moved."""
    idea = ideas[at]
    if idea.reading is not None:
        return idea.landed
    return 10.0 * at if idea.outcome == "running" else idea.landed + 1


def _plot_order(ideas: list[_Idea]) -> list[int]:
    return sorted(range(len(ideas)), key=lambda at: (_landed(ideas, at), at))


def chart_of(ideas: list[_Idea], direction: str = "max") -> list[tuple[float | None, IdeaVerdict]]:
    """What the run page's chart says of ``ideas``, stated plainly.

    Ideas that read a value are plotted in the order the readings landed (ties in the order they
    began), the others where their workstream ended (where it began, while it runs). The input stands until a passed idea beats it; that idea is
    kept and becomes the standing result. A passed idea that does not is not better; failing and
    running are the workstream's own outcome, whatever the reading.
    """

    order = _plot_order(ideas)
    standing = _INPUT if any(idea.reading is not None for idea in ideas) else None
    chart: list[tuple[float | None, IdeaVerdict]] = []
    for at in order:
        idea = ideas[at]
        reading = idea.reading
        ratio = (
            None
            if reading is None
            else (_INPUT / reading if direction == "min" else reading / _INPUT)
        )
        if idea.outcome == "running":
            chart.append((ratio, IdeaVerdict.RUNNING))
        elif idea.outcome == "failed":
            chart.append((ratio, IdeaVerdict.FAILED))
        elif reading is not None and (
            standing is None or (reading < standing if direction == "min" else reading > standing)
        ):
            standing = reading
            chart.append((ratio, IdeaVerdict.KEPT))
        else:
            chart.append((ratio, IdeaVerdict.NOT_BETTER))
    return chart


_IDEAS = st.lists(
    st.builds(
        _Idea,
        st.sampled_from(["passed", "passed", "failed", "running"]),
        st.none() | st.integers(10, 300).map(float),
        st.integers(0, 400).map(float),
    ),
    max_size=12,
)
_DIRECTIONS = st.sampled_from(["max", "min"])


@example(ideas=[_Idea("running", None, 0.0), _Idea("running", 10.0, 0.0)], direction="max")
@example(ideas=[_Idea("running", None, 0.0), _Idea("passed", 10.0, 0.0)], direction="max")
@given(ideas=_IDEAS, direction=_DIRECTIONS)
def test_the_digests_points_are_the_run_pages_chart_over_any_run(
    ideas: list[_Idea], direction: str
) -> None:
    digest = digest_journal(journal_of(ideas, direction))

    assert digest.dashboard is not None
    got = [(point.ratio, point.verdict) for point in digest.dashboard.points]
    want = chart_of(ideas, direction)
    assert [verdict for _, verdict in got] == [verdict for _, verdict in want]
    for (ratio, _), (expected, _) in zip(got, want, strict=True):
        assert ratio == pytest.approx(expected)
    assert digest.ideas == len(ideas)
    assert digest.dashboard.points_omitted == 0


@given(ideas=_IDEAS)
def test_each_point_says_when_its_result_landed_by_the_journals_clock(ideas: list[_Idea]) -> None:
    digest = digest_journal(journal_of(ideas))

    assert digest.dashboard is not None
    got = [point.at for point in digest.dashboard.points]
    want = [datetime.fromtimestamp(_BASE + _landed(ideas, at), UTC) for at in _plot_order(ideas)]
    assert got == want


@given(ideas=_IDEAS, direction=_DIRECTIONS)
def test_the_headline_while_the_run_works_is_the_last_kept_point(
    ideas: list[_Idea], direction: str
) -> None:
    digest = digest_journal(journal_of(ideas, direction))

    assert digest.dashboard is not None
    kept = [point.ratio for point in digest.dashboard.points if point.verdict is IdeaVerdict.KEPT]
    assert digest.best_so_far_ratio == (pytest.approx(kept[-1]) if kept else None)


@example(
    ideas=[_Idea("passed", 113.0, 0.0), _Idea("passed", 100.0, 0.0)],
    direction="min",
    metric=None,
    unit=None,
)
@given(
    ideas=_IDEAS,
    direction=_DIRECTIONS,
    metric=st.none() | st.just("output_tokens_per_s"),
    unit=st.none() | st.just("tok/s"),
)
def test_the_best_reading_is_the_last_kept_ideas_value_in_the_runs_own_metric(
    ideas: list[_Idea], direction: str, metric: str | None, unit: str | None
) -> None:
    digest = digest_journal(journal_of(ideas, direction, metric, unit))

    assert digest.dashboard is not None
    order = _plot_order(ideas)
    kept = [
        ideas[at].reading
        for at, (_, verdict) in zip(order, chart_of(ideas, direction), strict=True)
        if verdict is IdeaVerdict.KEPT
    ]
    expected = (
        BestReading(value=kept[-1], metric=metric, unit=unit)
        if kept and kept[-1] is not None
        else None
    )
    assert digest.dashboard.best == expected


def test_a_run_whose_input_was_never_measured_still_reads_its_best_value() -> None:
    """The run page shows the value when no ratio exists: so does the digest (the live-run regression)."""
    lines = journal_of(
        [_Idea("passed", 287.0, 5.0)], metric="output_tokens_per_s", input_measured=False
    )

    digest = digest_journal(lines)

    assert digest.dashboard is not None
    assert digest.dashboard.points[0].ratio is None
    assert digest.dashboard.best == BestReading(
        value=287.0, metric="output_tokens_per_s", unit=None
    )


@given(ideas=_IDEAS)
def test_every_kept_idea_beats_the_kept_ideas_before_it(ideas: list[_Idea]) -> None:
    digest = digest_journal(journal_of(ideas))

    assert digest.dashboard is not None
    kept = [p.ratio for p in digest.dashboard.points if p.verdict is IdeaVerdict.KEPT]
    ratios = [ratio for ratio in kept if ratio is not None]
    assert len(ratios) == len(kept)
    assert all(ratio > 1.0 for ratio in ratios)
    assert ratios == sorted(set(ratios))


@given(
    ideas=st.lists(
        st.builds(
            _Idea,
            st.sampled_from(["passed", "failed", "running"]),
            st.integers(10, 300).map(float),
            st.integers(0, 4000).map(float),
        ),
        min_size=DASHBOARD_MAX_POINTS - 3,
        max_size=DASHBOARD_MAX_POINTS * 2,
    )
)
def test_a_long_run_keeps_at_most_the_cap_and_says_how_many_it_left_out(
    ideas: list[_Idea],
) -> None:
    digest = digest_journal(journal_of(ideas))
    whole = chart_of(ideas)

    assert digest.dashboard is not None
    points = digest.dashboard.points
    assert len(points) <= DASHBOARD_MAX_POINTS
    assert len(points) + digest.dashboard.points_omitted == len(ideas)
    kept_before = [verdict for _, verdict in whole].count(IdeaVerdict.KEPT)
    kept_after = [point.verdict for point in points].count(IdeaVerdict.KEPT)
    # the step line is dropped last: a kept point goes only when the kept ones alone are too many
    assert kept_after == min(kept_before, DASHBOARD_MAX_POINTS) or len(points) == len(whole)


class _Point(BaseModel):
    ratio: float | None
    verdict: IdeaVerdict


class _Scenario(BaseModel):
    name: str
    direction: str
    ideas: list[_Idea]
    points: list[_Point]


class _Corpus(BaseModel):
    scenarios: list[_Scenario]


_SCENARIOS = _Corpus.model_validate_json(
    (
        Path(__file__).parents[1] / "conformance" / "fixtures" / "run-dashboard" / "scenarios.json"
    ).read_text()
).scenarios


def _approx(ratio: float | None) -> object:
    return None if ratio is None else pytest.approx(ratio)


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=[each.name for each in _SCENARIOS])
def test_the_digest_draws_each_shared_scenario_as_the_corpus_says(scenario: _Scenario) -> None:
    digest = digest_journal(journal_of(scenario.ideas, scenario.direction))

    assert digest.dashboard is not None
    got = [(point.ratio, point.verdict) for point in digest.dashboard.points]
    assert got == [(_approx(point.ratio), point.verdict) for point in scenario.points]


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=[each.name for each in _SCENARIOS])
def test_the_statement_the_property_tests_use_agrees_with_the_corpus(scenario: _Scenario) -> None:
    """The corpus pins ``chart_of``, so holding the digest to it holds the digest to the corpus."""
    assert chart_of(scenario.ideas, scenario.direction) == [
        (_approx(point.ratio), point.verdict) for point in scenario.points
    ]


def test_a_passed_idea_slower_than_the_input_is_not_better_and_a_minimized_one_reads_inverted() -> (
    None
):
    ideas = [_Idea("passed", 80.0, 5.0), _Idea("passed", 50.0, 9.0)]

    maximized = digest_journal(journal_of(ideas, "max")).dashboard
    minimized = digest_journal(journal_of(ideas, "min")).dashboard

    assert maximized is not None
    assert minimized is not None
    assert [p.verdict for p in maximized.points] == [IdeaVerdict.NOT_BETTER] * 2
    assert [p.verdict for p in minimized.points] == [IdeaVerdict.KEPT] * 2
    assert [p.ratio for p in minimized.points] == [pytest.approx(1.25), pytest.approx(2.0)]


@given(
    tail=st.lists(st.sampled_from(["failed", "passed", "running"]), max_size=8),
    earlier=st.integers(0, 4),
)
def test_the_failed_streak_counts_the_ideas_that_failed_since_one_last_passed(
    tail: list[str], earlier: int
) -> None:
    ideas = [
        _Idea(outcome, None, float(at * 3))
        for at, outcome in enumerate(["failed"] * earlier + tail)
    ]
    streak = 0
    for outcome in reversed([idea.outcome for idea in ideas]):
        if outcome == "running":
            continue
        if outcome != "failed":
            break
        streak += 1

    digest = digest_journal(journal_of(ideas))

    assert digest.dashboard is not None
    assert digest.dashboard.failed_streak == streak


def _digest(*, streak: int = 0, last: float | None = 0.0, **facts: object) -> RunDigest:
    dashboard = RunDashboard(
        points=(),
        points_omitted=0,
        failed_streak=streak,
        last_event_at=None if last is None else datetime.fromtimestamp(last, UTC),
    )
    base = {
        "ideas": 0,
        "adopted_ratio": None,
        "best_so_far_ratio": None,
        "duration_s": None,
        "ending": None,
        "closing": None,
        "dashboard": dashboard,
    }
    return RunDigest.model_validate({**base, **facts})


_RUNNING = RunPhase(status=RunStatus.RUNNING)


@given(now=st.floats(0, 1e6))
def test_a_working_run_is_stalled_exactly_when_its_journal_is_older_than_the_threshold(
    now: float,
) -> None:
    attention = run_attention(_digest(last=0.0), phase=_RUNNING, now=now)

    assert (attention is RunAttention.STALLED) == (now - 0.0 > STALLED_AFTER_S)


def test_a_paused_run_or_one_with_no_server_is_never_stalled() -> None:
    way_later = 10 * STALLED_AFTER_S

    paused = run_attention(_digest(), phase=RunPhase(status=RunStatus.PAUSED), now=way_later)
    orphan = run_attention(_digest(), phase=None, now=way_later)

    assert paused is orphan is RunAttention.NONE


@given(streak=st.integers(0, 10))
def test_a_working_run_whose_last_ideas_failed_asks_for_attention_at_the_threshold(
    streak: int,
) -> None:
    attention = run_attention(_digest(streak=streak, last=0.0), phase=_RUNNING, now=1.0)

    assert (attention is RunAttention.REPEATED_FAILURE) == (streak >= REPEATED_FAILURE_AFTER)


def test_a_failed_run_needs_attention_whatever_the_time_and_an_ended_one_otherwise_does_not() -> (
    None
):
    failed_closing = _digest(closing=RunClosing.FAILED)
    failed_ending = _digest(
        ending=RunEndingFact(
            outcome=RunEndOutcome.FAILED, summary="", resumable=True, rounds_finished=0
        )
    )
    failed_phase = RunPhase(
        status=RunStatus.FAILED, reason=FailureReason(code="x", stage="start", message="m")
    )

    assert run_attention(failed_closing, phase=None, now=0.0) is RunAttention.FAILED
    assert run_attention(failed_ending, phase=None, now=0.0) is RunAttention.FAILED
    assert run_attention(_digest(), phase=failed_phase, now=0.0) is RunAttention.FAILED
    finished = _digest(closing=RunClosing.FINISHED, streak=9)
    assert run_attention(finished, phase=_RUNNING, now=1e9) is RunAttention.NONE
    stopped = _digest(closing=RunClosing.INTERRUPTED, streak=9)
    assert run_attention(stopped, phase=None, now=1e9) is RunAttention.NONE


def _list(project: Path, *, with_dashboard: bool | None) -> RunsListReply:
    threads = SimThreads()
    host = fake_host(threads, SimNetwork(threads))
    request = RunsListRequest(
        request_id="l",
        deadline_ms=5000,
        project=str(project),
        digest_runs=(RUN_ID,),
        with_dashboard=with_dashboard,
    )
    reply = answer(request, host, threads.event())
    assert isinstance(reply, RunsListReply)
    return reply


def _journal_in(project: Path, lines: list[bytes]) -> None:
    journal = Project.log_directory_for(project.resolve(), RUN_ID) / RUN_EVENTS_FILE
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes(b"".join(lines))


def test_the_dashboard_and_the_attention_reach_only_a_client_that_asked_for_them(
    tmp_path: Path,
) -> None:
    project = project_with_run(tmp_path / "proj", RUN_ID)
    _journal_in(project, journal_of([_Idea("passed", 150.0, 5.0)]))

    plain = _list(project, with_dashboard=None).runs.runs[0]
    asked = _list(project, with_dashboard=True).runs.runs[0]

    assert isinstance(plain.digest, RunDigest)
    assert plain.digest.dashboard is None
    assert plain.attention is None
    assert "dashboard" not in plain.model_dump(mode="json", exclude_none=False)["digest"]
    assert isinstance(asked.digest, RunDigest)
    assert asked.digest.dashboard is not None
    assert [p.verdict for p in asked.digest.dashboard.points] == [IdeaVerdict.KEPT]
    assert asked.attention is RunAttention.NONE


def test_asking_for_dashboards_needs_the_run_dashboard_feature() -> None:
    asked = RunsListRequest(
        request_id="l", deadline_ms=1, project="/p", digest_runs=("r",), with_dashboard=True
    )

    assert [feature.value for feature in request_features(asked)] == [
        "run_digest",
        "run_dashboard",
    ]


def test_the_largest_runs_list_stays_far_under_a_helper_line() -> None:
    """100 runs of the most points each, with the longest numbers, fit a line with room to spare."""
    ideas = [_Idea("passed", 100.0 + at / 7.0, float(at)) for at in range(DASHBOARD_MAX_POINTS * 2)]
    digest = digest_journal(journal_of(ideas))

    reply = RunsListReply(
        request_id="l",
        runs={
            "project_root": "/p",
            "runs": [
                {
                    "run_id": RUN_ID,
                    "task": None,
                    "display_name": "x" * 80,
                    "created_at": "2026-10-10T12:00:00Z",
                    "phase": None,
                    "instance_id": None,
                    "digest": digest,
                    "attention": RunAttention.NONE,
                }
            ]
            * 100,
        },
    )

    assert len(reply.model_dump_json()) < HELPER_LINE_MAX_BYTES // 2
