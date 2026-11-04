"""A real-process kill or pause is placed by what the run reported, never by a job on the cluster.

The first job a run submits is its input's measurement, and it goes out as the run starts, beside
the planner. A test that interrupts the run "once a job is pending" therefore interrupts it
anywhere from the planner's turn to the judge's step, depending on how the scheduler ran the
processes that moment, and each place is a different recovery: the case passed or failed with the
timing, and the resumed run was silent or ended on other facts. The job on the cluster proves
nothing about the run, so a test waits for it only after the run reported the step it wants to be
past (`run.wait_for`), in one helper.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from tests.support.parsed_source import STATIC_SCAN_GROUP, parse_source, python_files, walk_nodes

pytestmark = pytest.mark.xdist_group(STATIC_SCAN_GROUP)

REPO = Path(__file__).resolve().parents[2]
CLUSTER_POLL = "wait_for_pending_job"
REPORT_WAIT = "wait_for"


def _calls(node: ast.AST, attribute: str) -> list[ast.Call]:
    return [
        call
        for call in walk_nodes(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == attribute
    ]


def polls_before_the_run_reported(source: str) -> list[int]:
    """Lines of a cluster poll that no wait for the run's own report precedes in its function."""
    lines: list[int] = []
    for function in walk_nodes(parse_source(source)):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        reported = [call.lineno for call in _calls(function, REPORT_WAIT)]
        lines += [
            poll.lineno
            for poll in _calls(function, CLUSTER_POLL)
            if not any(at < poll.lineno for at in reported)
        ]
    return lines


def test_the_checker_flags_a_poll_the_run_did_not_precede() -> None:
    bare = (
        "def case(scenario):\n    first = scenario.start()\n    scenario.wait_for_pending_job()\n"
    )
    placed = (
        "def wait(scenario, run):\n    run.wait_for(done)\n    scenario.wait_for_pending_job()\n"
    )

    assert polls_before_the_run_reported(bare) == [3]
    assert polls_before_the_run_reported(placed) == []
    assert polls_before_the_run_reported("def case(run):\n    run.wait_for(done)\n") == []


def test_no_real_process_case_interrupts_a_run_by_a_pending_job_alone() -> None:
    offenders = [
        f"{path.relative_to(REPO)}:{line}"
        for path in python_files(REPO / "tests" / "e2e")
        for line in polls_before_the_run_reported(path.read_text(encoding="utf-8"))
    ]

    assert offenders == [], "wait for the run's report of the step first, then for the job"
