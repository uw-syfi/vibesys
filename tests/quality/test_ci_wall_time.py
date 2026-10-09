"""Tests for the CI execution-time judge behind the `ci-budget` job.

The contract: a run's queue time (jobs waiting for a runner) must never decide
its verdict; only the execution time along the critical path does. Job graphs
are synthetic API records, so the assertions need no network and no clock.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st
from scripts.ci_wall_time import analyze, job_from_record, main, render

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from scripts.ci_wall_time import Job

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
    last = max(range(len(graph)), key=lambda i: sum(graph[i]))
    created, wait, ran = graph[last]
    delayed = list(graph)
    delayed[last] = (created, wait + extra, ran)

    before = analyze(jobs_of(records_of(graph)))
    after = analyze(jobs_of(records_of(delayed)))

    assert before is not None
    assert after is not None
    assert after.execution == before.execution
    assert after.queue == before.queue + extra
