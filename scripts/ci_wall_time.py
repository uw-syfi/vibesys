#!/usr/bin/env python3
r"""Judge a workflow run's wall time by execution, not by runner queue time.

The `ci-budget` job in `.github/workflows/test.yml` feeds this script the run's
jobs from the GitHub API. A run's elapsed time mixes two things a workflow
change can only influence one of: the time jobs spend executing, and the time
they wait for a free runner because other runs share the pool. The jobs API
separates them. A job's `created_at` is when it became ready (its `needs` are
done), and `started_at` is when a runner picked it up, so `started_at -
created_at` is pure runner wait and the wait for dependencies is not part of it.

The report follows the critical path: from the last job to finish, walk back to
the job whose completion made it ready. Elapsed is the span of that path, queue
is the runner wait summed along it, and execution is the difference. Only
execution is compared with the warn and error thresholds; queue stays visible in
the summary and the annotation so contention is seen without failing the run.

Usage:
    gh api "repos/$REPO/actions/runs/$RUN_ID/jobs?per_page=100" --paginate \\
        --jq '.jobs[]' > jobs.ndjson
    python3 scripts/ci_wall_time.py jobs.ndjson --warn 360 --error 600
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# A job becomes ready a moment after its last dependency completes; timestamps
# have one-second resolution, so allow that much slack when matching them.
READY_EPSILON_SECONDS = 2.0


@dataclass(frozen=True)
class Job:
    """One finished job, with the three instants the API reports for it."""

    name: str
    conclusion: str
    created_at: datetime
    started_at: datetime
    completed_at: datetime

    @property
    def runner_wait(self) -> float:
        """Seconds between becoming ready and a runner picking the job up."""
        return max(0.0, (self.started_at - self.created_at).total_seconds())

    @property
    def ran(self) -> float:
        """Seconds the job spent on a runner."""
        return max(0.0, (self.completed_at - self.started_at).total_seconds())


@dataclass(frozen=True)
class Report:
    """Wall time of a run split into runner queue and execution."""

    jobs: tuple[Job, ...]
    critical_path: tuple[Job, ...]
    elapsed: float
    queue: float
    execution: float


def parse_moment(value: str | None) -> datetime | None:
    """Parse an API timestamp; an absent or empty value is None."""
    return datetime.fromisoformat(value) if value else None


def job_from_record(record: dict[str, object]) -> Job | None:
    """Build a Job from an API record, or None when it cost the run nothing.

    Skipped jobs report timestamps that do not describe real work, and a job
    that is still running (the caller itself) has no completed_at.
    """
    created = parse_moment(_text(record.get("created_at")))
    started = parse_moment(_text(record.get("started_at")))
    completed = parse_moment(_text(record.get("completed_at")))
    conclusion = _text(record.get("conclusion")) or ""
    if conclusion == "skipped" or created is None or started is None or completed is None:
        return None
    return Job(_text(record.get("name")) or "", conclusion, created, started, completed)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def analyze(jobs: list[Job], epsilon: float = READY_EPSILON_SECONDS) -> Report | None:
    """Split the run's wall time along its critical path; None for no jobs."""
    if not jobs:
        return None
    current = max(jobs, key=lambda job: job.completed_at)
    path = [current]
    while True:
        ready = current.created_at
        predecessors = [
            job
            for job in jobs
            # Strictly earlier completion also guarantees the walk terminates.
            if job.completed_at < current.completed_at
            and (job.completed_at - ready).total_seconds() <= epsilon
        ]
        if not predecessors:
            break
        # The dependency that finished last is the one that released this job.
        current = max(predecessors, key=lambda job: job.completed_at)
        path.append(current)
    path.reverse()
    elapsed = (path[-1].completed_at - path[0].created_at).total_seconds()
    queue = min(elapsed, sum(job.runner_wait for job in path))
    return Report(tuple(jobs), tuple(path), elapsed, queue, elapsed - queue)


def render(report: Report, warn: float, error: float) -> tuple[str, str, int]:
    """Return (step summary markdown, workflow-command line, exit code)."""
    start = report.critical_path[0].created_at
    on_path = {id(job) for job in report.critical_path}
    lines = [
        f"## CI execution time: {report.execution:.0f}s (warn {warn:.0f}s, error {error:.0f}s)",
        "",
        f"Elapsed {report.elapsed:.0f}s, of which {report.queue:.0f}s was waiting for a runner"
        f" on the critical path. Only execution ({report.execution:.0f}s) is compared with the"
        " thresholds; runner wait depends on how busy the shared runner pool is.",
        "",
        "| job | result | runner wait (s) | ran (s) | finished at (s) | critical path |",
        "| --- | --- | ---: | ---: | ---: | :---: |",
    ]
    for job in sorted(report.jobs, key=lambda item: item.completed_at, reverse=True):
        finished = (job.completed_at - start).total_seconds()
        marker = "yes" if id(job) in on_path else ""
        lines.append(
            f"| {job.name} | {job.conclusion or 'n/a'} | {job.runner_wait:.0f} | {job.ran:.0f}"
            f" | {finished:.0f} | {marker} |"
        )
    last = report.critical_path[-1]
    detail = (
        f"Elapsed {report.elapsed:.0f}s = {report.queue:.0f}s runner queue +"
        f" {report.execution:.0f}s execution. Long pole: {last.name}"
        f" ({last.runner_wait:.0f}s queued, {last.ran:.0f}s running)."
    )
    if report.execution > error:
        message = (
            f"::error title=CI over execution-time limit::Execution took"
            f" {report.execution:.0f}s, over the {error:.0f}s limit. {detail}"
        )
        return "\n".join(lines) + "\n", message, 1
    if report.execution > warn:
        message = (
            f"::warning title=CI over execution-time budget::Execution took"
            f" {report.execution:.0f}s, over the {warn:.0f}s warning threshold. {detail}"
        )
        return "\n".join(lines) + "\n", message, 0
    message = f"Execution took {report.execution:.0f}s, within the {warn:.0f}s warning threshold. {detail}"
    return "\n".join(lines) + "\n", message, 0


def load_jobs(path: Path) -> list[Job]:
    """Read newline-delimited job records, as `gh api --jq '.jobs[]'` prints them."""
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [job for record in records if (job := job_from_record(record)) is not None]


def main(argv: list[str] | None = None) -> int:
    """Run the CLI; the exit code is 1 when execution exceeds the error threshold."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("jobs_file", type=Path, help="newline-delimited job records")
    parser.add_argument("--warn", type=float, required=True, help="execution seconds to warn above")
    parser.add_argument("--error", type=float, required=True, help="execution seconds to fail above")
    args = parser.parse_args(argv)

    report = analyze(load_jobs(args.jobs_file))
    if report is None:
        print("Every job in this run was skipped; nothing to measure.")
        return 0
    summary, message, code = render(report, args.warn, args.error)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as handle:
            handle.write(summary)
    else:
        print(summary)
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
