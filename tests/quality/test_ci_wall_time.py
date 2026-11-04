"""Tests for the CI execution-time judge behind the `ci-budget` job.

The contract: a run's queue time (jobs waiting for a runner) must never decide
its verdict; only the execution time along the critical path does. Job graphs
are synthetic API records, so the assertions need no network and no clock.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st
from scripts.ci_wall_time import (
    READY_EPSILON_SECONDS,
    analyze,
    job_from_record,
    main,
    render,
)

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.ci_wall_time import Job, Report

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def stamp(seconds: float | None) -> str:
    if seconds is None:
        return ""
    return (EPOCH + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def record(
    name: str,
    created: float,
    wait: float,
    ran: float,
    conclusion: str | None = "success",
) -> dict[str, object]:
    """An API job record that became ready at ``created``, waited, then ran."""
    return {
        "name": name,
        "conclusion": conclusion,
        "created_at": stamp(created),
        "started_at": stamp(created + wait),
        "completed_at": stamp(created + wait + ran),
    }


def with_steps(item: dict[str, object]) -> dict[str, object]:
    """Add the steps a runner reports: they start just after the job and end just before it."""
    return {
        **item,
        "steps": [
            {
                "name": "Set up job",
                "started_at": item["started_at"],
                "completed_at": item["started_at"],
            },
            {
                "name": "Complete job",
                "started_at": item["completed_at"],
                "completed_at": item["completed_at"],
            },
        ],
    }


def jobs_of(records: list[dict[str, object]]) -> list[Job]:
    return [job for item in records if (job := job_from_record(item)) is not None]


def test_a_run_with_no_queue_reports_its_elapsed_time_as_execution() -> None:
    report = analyze(jobs_of([record("changes", 0, 0, 10), record("test", 10, 0, 100)]))

    assert report is not None
    assert (report.elapsed, report.queue, report.execution) == (110, 0, 110)


def test_queue_on_the_critical_path_is_not_execution() -> None:
    report = analyze(jobs_of([record("changes", 0, 5, 10), record("test", 15, 700, 100)]))

    assert report is not None
    assert (report.elapsed, report.queue, report.execution) == (815, 705, 110)
    assert [job.name for job in report.critical_path] == ["changes", "test"]


def test_queue_off_the_critical_path_is_ignored() -> None:
    report = analyze(
        jobs_of(
            [
                record("changes", 0, 0, 10),
                record("test", 10, 0, 200),
                record("lint", 10, 100, 5),  # queued long, but finishes before test
            ]
        )
    )

    assert report is not None
    assert (report.queue, report.execution) == (0, 210)
    assert "lint" not in [job.name for job in report.critical_path]


def test_parallel_branches_follow_the_slowest() -> None:
    report = analyze(
        jobs_of(
            [
                record("changes", 0, 0, 10),
                record("fast", 10, 0, 20),
                record("slow", 10, 40, 60),
                record("gate", 110, 0, 5),
            ]
        )
    )

    assert report is not None
    assert [job.name for job in report.critical_path] == ["changes", "slow", "gate"]
    assert (report.elapsed, report.queue, report.execution) == (115, 40, 75)


def test_skipped_jobs_and_jobs_without_completed_at_are_ignored() -> None:
    jobs = jobs_of(
        [
            record("changes", 0, 0, 10),
            record("skipped", 0, 0, -50, conclusion="skipped"),
            {**record("self", 10, 0, 0, conclusion=None), "completed_at": ""},
        ]
    )

    assert [job.name for job in jobs] == ["changes"]


def test_a_job_that_started_before_its_creation_is_ignored() -> None:
    inherited = {**record("inherited", 0, 0, 110), "created_at": stamp(100)}

    assert job_from_record(inherited) is None


def test_a_job_that_completed_before_its_creation_is_ignored() -> None:
    inherited: dict[str, object] = {
        "name": "inherited",
        "conclusion": "success",
        "created_at": stamp(100),
        "started_at": stamp(110),
        "completed_at": stamp(90),
    }

    assert job_from_record(inherited) is None


def test_a_partial_rerun_ignores_jobs_inherited_from_the_previous_attempt() -> None:
    inherited = {**record("inherited", 0, 0, 50), "created_at": stamp(107)}
    report = analyze(
        jobs_of(
            [
                inherited,
                record("rerun", 100, 3, 97),
                record("gate", 200, 2, 8),
            ]
        )
    )

    assert report is not None
    assert [job.name for job in report.jobs] == ["rerun", "gate"]
    assert [job.name for job in report.critical_path] == ["rerun", "gate"]
    assert (report.elapsed, report.queue, report.execution) == (110, 5, 105)


def test_a_completion_far_before_readiness_is_not_a_predecessor() -> None:
    report = analyze(
        jobs_of(
            [
                record("earlier", 0, 0, 10),
                record("later", 100, 0, 10),
            ]
        )
    )

    assert report is not None
    assert [job.name for job in report.critical_path] == ["later"]
    assert (report.elapsed, report.queue, report.execution) == (10, 0, 10)


def test_a_single_job_is_its_own_critical_path() -> None:
    report = analyze(jobs_of([record("only", 0, 30, 20)]))

    assert report is not None
    assert (report.elapsed, report.queue, report.execution) == (50, 30, 20)


def test_no_measurable_jobs_has_no_report() -> None:
    assert analyze(jobs_of([record("skipped", 0, 0, 0, conclusion="skipped")])) is None


def test_contention_alone_never_fails_the_run() -> None:
    report = analyze(jobs_of([record("changes", 0, 900, 10), record("test", 910, 900, 100)]))
    assert report is not None

    summary, message, code = render(report, warn=360, error=600)

    assert code == 0
    assert message.startswith("Execution took 110s")
    assert "1800s runner queue" in message
    assert "runner wait" in summary


def test_slow_execution_warns_then_fails_and_still_names_the_queue() -> None:
    warn_report = analyze(jobs_of([record("test", 0, 50, 400)]))
    error_report = analyze(jobs_of([record("test", 0, 50, 650)]))
    assert warn_report is not None
    assert error_report is not None

    _, warning, warn_code = render(warn_report, warn=360, error=600)
    _, error, error_code = render(error_report, warn=360, error=600)

    assert (warn_code, error_code) == (0, 1)
    assert warning.startswith("::warning")
    assert "50s runner queue" in warning
    assert error.startswith("::error")
    assert "50s runner queue" in error


def test_long_pole_is_the_slowest_executing_job_not_the_path_tail() -> None:
    report = analyze(
        jobs_of(
            [
                record("test", 0, 0, 650),
                record("required gate", 650, 0, 2),
            ]
        )
    )
    assert report is not None

    _, message, _ = render(report, warn=360, error=600)

    assert "Long pole: test (0s queued, 650s running)" in message


def test_cli_writes_the_summary_and_returns_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ndjson = tmp_path / "jobs.ndjson"
    records = [record("changes", 0, 0, 10), record("test", 10, 0, 700)]
    ndjson.write_text("\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    code = main([str(ndjson), "--warn", "360", "--error", "600"])

    assert code == 1
    assert "| test | success | 0 | 700 |" in summary.read_text(encoding="utf-8")


@st.composite
def dags(draw: st.DrawFn) -> list[tuple[int, int, int]]:
    """Jobs as (created, wait, ran); each is created when its dependencies finish."""
    size = draw(st.integers(1, 12))
    finish: list[int] = []
    graph = []
    for index in range(size):
        deps = draw(st.sets(st.sampled_from(range(index)), max_size=3)) if index else set()
        created = max((finish[dep] for dep in deps), default=0)
        wait = draw(st.integers(0, 60))
        ran = draw(st.integers(1, 600))
        finish.append(created + wait + ran)
        graph.append((created, wait, ran))
    return graph


def records_of(graph: list[tuple[int, int, int]]) -> list[dict[str, object]]:
    return [record(f"j{i}", c, w, r) for i, (c, w, r) in enumerate(graph)]


@given(
    st.lists(
        st.tuples(
            st.integers(0, 3000),
            st.integers(0, 60),
            st.integers(1, 600),
        ),
        min_size=1,
        max_size=12,
    )
)
def test_every_critical_path_edge_meets_at_readiness(
    graph: list[tuple[int, int, int]],
) -> None:
    report = analyze(jobs_of(records_of(graph)))

    assert report is not None
    for predecessor, successor in pairwise(report.critical_path):
        ready_delta = (predecessor.completed_at - successor.created_at).total_seconds()
        assert abs(ready_delta) <= READY_EPSILON_SECONDS


@given(dags())
def test_elapsed_is_the_last_finish_and_splits_into_queue_and_execution(
    graph: list[tuple[int, int, int]],
) -> None:
    report = analyze(jobs_of(records_of(graph)))

    assert report is not None
    assert report.elapsed == max(c + w + r for c, w, r in graph)
    assert 0 <= report.queue <= sum(w for _, w, _ in graph)
    assert report.execution == report.elapsed - report.queue


@given(dags(), st.integers(1, 3000))
def test_extra_runner_wait_moves_time_from_execution_to_queue_only(
    graph: list[tuple[int, int, int]], extra: int
) -> None:
    """Delaying the last job by queueing changes elapsed and queue, never execution."""
    finish = [sum(job) for job in graph]
    # With two jobs ending together, either is "the last"; the report follows the one the
    # analysis picks, so the property is about a graph with a single last job.
    assume(finish.count(max(finish)) == 1)
    last = finish.index(max(finish))
    created, wait, ran = graph[last]
    delayed = list(graph)
    delayed[last] = (created, wait + extra, ran)

    before = analyze(jobs_of(records_of(graph)))
    after = analyze(jobs_of(records_of(delayed)))

    assert before is not None
    assert after is not None
    assert after.execution == before.execution
    assert after.queue == before.queue + extra


CLOCK_CHANGE_SECONDS = 3600


def as_the_api_reports_the_repeated_hour(
    records: list[dict[str, object]], window_start: int
) -> list[dict[str, object]]:
    """Job-level times inside the repeated hour read an hour ahead; step times stay true.

    The API did this on 2026-11-01, when daylight saving time ended: a five-minute run
    of three jobs ended 3901 s after it began.
    """

    def shifted(value: object) -> str:
        moment = datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        seconds = (moment - EPOCH).total_seconds()
        if window_start <= seconds < window_start + CLOCK_CHANGE_SECONDS:
            seconds += CLOCK_CHANGE_SECONDS
        return stamp(seconds)

    return [
        {
            **item,
            "created_at": shifted(item["created_at"]),
            "started_at": shifted(item["started_at"]),
            "completed_at": shifted(item["completed_at"]),
        }
        for item in records
    ]


def the_run_in(report: Report | None) -> tuple[float, float, float, list[str]]:
    assert report is not None
    path = [job.name for job in report.critical_path]
    return report.elapsed, report.queue, report.execution, path


@pytest.mark.regression
@example(graph=[(0, 2, 20), (22, 2, 10), (34, 2, 8)], window_start=21)
@given(dags(), st.integers(0, 9000))
def test_a_repeated_hour_of_a_clock_change_never_changes_the_run_it_reports(
    graph: list[tuple[int, int, int]], window_start: int
) -> None:
    """A job the API stamps an hour ahead is judged by its steps, so the run reads as it was."""
    true_records = [with_steps(item) for item in records_of(graph)]
    reported = as_the_api_reports_the_repeated_hour(true_records, window_start)
    # A job queued over the end of the repeated hour is created an hour late, after it
    # started, and is dropped as one inherited from an earlier attempt would be.
    assume(all(job_from_record(item) is not None for item in reported))

    assert the_run_in(analyze(jobs_of(reported))) == the_run_in(analyze(jobs_of(true_records)))


def test_the_failing_run_of_the_night_daylight_saving_time_ended_reads_as_five_minutes() -> None:
    """The three jobs of run 41286944370: real step times, job times an hour ahead from 08:00."""

    def job(
        name: str, job_times: tuple[str, str, str], step_times: tuple[str, str]
    ) -> dict[str, object]:
        created, started, completed = job_times
        first, last = step_times
        return {
            "name": name,
            "conclusion": "success",
            "created_at": created,
            "started_at": started,
            "completed_at": completed,
            "steps": [
                {"started_at": first, "completed_at": first},
                {"started_at": last, "completed_at": last},
            ],
        }

    records = [
        job(
            "changes",
            ("2026-11-01T07:59:28Z", "2026-11-01T07:59:30Z", "2026-11-01T07:59:49Z"),
            ("2026-11-01T07:59:31Z", "2026-11-01T07:59:48Z"),
        ),
        job(
            "clients-quality",
            ("2026-11-01T07:59:50Z", "2026-11-01T07:59:52Z", "2026-11-01T09:04:27Z"),
            ("2026-11-01T07:59:53Z", "2026-11-01T08:04:25Z"),
        ),
        job(
            "ci-budget",
            ("2026-11-01T09:04:27Z", "2026-11-01T09:04:30Z", "2026-11-01T09:04:36Z"),
            ("2026-11-01T08:04:31Z", "2026-11-01T08:04:34Z"),
        ),
    ]

    report = analyze(jobs_of(records))

    assert report is not None
    assert [item.name for item in report.critical_path] == [
        "changes",
        "clients-quality",
        "ci-budget",
    ]
    assert report.elapsed == 308
    assert report.execution == report.elapsed - report.queue


def test_a_job_that_waited_hours_is_not_taken_for_a_clock_change() -> None:
    """Only a whole-hour disagreement with the steps is the API's error; a long gap is real."""
    late = with_steps(record("late", 0, 0, 500))
    late["completed_at"] = stamp(500 + 1800)

    report = analyze(jobs_of([late]))

    assert report is not None
    assert report.elapsed == 500 + 1800
