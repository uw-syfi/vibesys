"""Every pytest CI job dumps the running test before its own timeout cancels it.

The per-test bound cannot see a job that is about to be cancelled: a hang that
starts late in a shard, or a slow test whose bound is longer than the job, is
killed with the runner and never named. The deadline is the job's
``timeout-minutes``, and the dump fires one margin before that.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from scripts.pytest_hang_dump import current_nodeid, render_dump, running_test
from scripts.pytest_job_deadline import (
    DUMP_MARGIN_S,
    SECONDS_PER_MINUTE,
    audit,
    dump_deadline,
    job_timeout_minutes,
    pytest_job_ids,
    workflow_gaps,
)

ROOT = Path(__file__).resolve().parents[2]
# Jobs whose cancel has already hidden a hang, plus the other pytest entry points.
# The audit still fails a new pytest job that is not in this set.
REQUIRED_JOBS = {
    ("nightly.yml", "chaos-sweep"),
    ("nightly.yml", "crash-sweep"),
    ("nightly.yml", "full-depth"),
    ("nightly.yml", "real-tiers"),
    ("test.yml", "agent-image"),
    ("test.yml", "clients-quality"),
    ("test.yml", "native-evaluator-examples"),
    ("test.yml", "python-checks"),
    ("test.yml", "real-tiers-minimal-container"),
    ("test.yml", "real-tiers-slurm"),
    ("test.yml", "sandbox-linux"),
    ("test.yml", "sandbox-macos"),
    ("test.yml", "seed-exploration"),
    ("test.yml", "test"),
    ("test.yml", "validate-examples"),
    ("test.yml", "validate-examples-cpu-check"),
}

_ARMED = """
on:
  push:
jobs:
  demo:
    timeout-minutes: 10
    steps:
      - name: Record when this job's cancel clock started
        run: echo "VIBESYS_JOB_STARTED_AT=$(date +%s)" >> "$GITHUB_ENV"
      - uses: actions/checkout@v1
      - name: Pass the pytest cancel deadline
        run: python3 scripts/pytest_job_deadline.py
      - name: Run
        run: uv run python -m pytest -q
"""

_BARE = """
jobs:
  demo:
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v1
      - name: Run
        run: uv run python -m pytest -q
"""


def test_every_pytest_ci_job_passes_its_deadline() -> None:
    assert pytest_job_ids(ROOT) >= REQUIRED_JOBS
    assert audit(ROOT) == []


def test_a_pytest_job_without_the_deadline_is_named() -> None:
    gaps = workflow_gaps("ci.yml", _BARE, set(), {})

    assert gaps
    assert all("demo" in gap for gap in gaps)
    assert any("pytest_job_deadline.py" in gap for gap in gaps)


def test_an_armed_job_is_not_a_gap() -> None:
    assert workflow_gaps("ci.yml", _ARMED, set(), {}) == []


def test_the_job_timeout_is_not_a_step_timeout() -> None:
    workflow = """
jobs:
  demo:
    timeout-minutes: 12
    steps:
      - name: Install
        timeout-minutes: 2
"""

    assert job_timeout_minutes(workflow, "demo") == 12


def test_a_margin_that_fills_the_job_is_a_gap() -> None:
    workflow = """
jobs:
  demo:
    timeout-minutes: 1
    steps:
      - name: Record when this job's cancel clock started
        run: echo "VIBESYS_JOB_STARTED_AT=$(date +%s)" >> "$GITHUB_ENV"
      - uses: actions/checkout@v1
      - name: Pass the pytest cancel deadline
        run: python3 scripts/pytest_job_deadline.py
      - name: Run
        run: uv run python -m pytest -q
"""

    gaps = workflow_gaps("ci.yml", workflow, set(), {})

    assert any("not inside" in gap for gap in gaps)


@pytest.mark.regression
@example(started=1_700_000_000, timeout_minutes=10)
@given(
    started=st.integers(min_value=0, max_value=2_000_000_000),
    timeout_minutes=st.integers(
        min_value=(DUMP_MARGIN_S // SECONDS_PER_MINUTE) + 1,
        max_value=24 * SECONDS_PER_MINUTE,
    ),
)
def test_the_dump_is_one_margin_before_the_job_cancel(started: int, timeout_minutes: int) -> None:
    # The reported hang is a 10 minute shard cancel (the `test` job) that
    # arrives before the per-test bound. The deadline is that cancel, minus
    # the margin, for every timeout the margin fits inside.
    cancel = started + timeout_minutes * SECONDS_PER_MINUTE
    deadline = dump_deadline(started, timeout_minutes)

    assert deadline == cancel - DUMP_MARGIN_S
    assert started < deadline < cancel


def test_the_dump_names_the_running_test_and_every_thread() -> None:
    frame = inspect.currentframe()
    assert frame is not None
    outer = current_nodeid()
    with running_test("tests/quality/test_pytest_job_deadline.py::test_one"):
        assert current_nodeid() == "tests/quality/test_pytest_job_deadline.py::test_one"
        text = render_dump(current_nodeid(), {7: frame, 8: frame}, {7: "MainThread", 8: "worker"})

    assert "running test: tests/quality/test_pytest_job_deadline.py::test_one" in text
    assert "Thread MainThread (7):" in text
    assert "Thread worker (8):" in text
    assert "test_the_dump_names_the_running_test_and_every_thread" in text
    assert current_nodeid() == outer


def test_the_dump_says_when_no_test_is_running() -> None:
    text = render_dump(None, {}, {})

    assert "running test: (no test is running)" in text
