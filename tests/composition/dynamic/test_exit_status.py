"""How a dynamic run ends: the exit status an operator and a script can rely on.

Success means the operator has a trusted, measured result to use. A run ends as
``adopted`` (a retained, verified candidate beat the input), ``no improvement``
(none did, but the input was measured and trusted) or ``no trusted result`` (the
input failed its measurement and no trusted candidate exists).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.composition.dynamic._harness import (
    PASS,
    CoreRecords,
    LoopInput,
    ScriptedAgents,
    edit_to,
    portfolio,
    run_request,
    workstream,
)

from vibesys.api import RunFailureKind, RunStatus
from vibesys.events import CoreEventType

if TYPE_CHECKING:
    from pathlib import Path

# VALUE = -1 fails the input project's accuracy check, so the candidate is never retained.
_FAILS_ACCURACY = -1


def _one_candidate(value: int) -> ScriptedAgents:
    return (
        ScriptedAgents()
        .plan(portfolio(workstream("H1")))
        .implement("H1", edit_to(value, "H1"))
        .judge("H1", PASS)
    )


def _failing_input(loop_input: LoopInput) -> None:
    (loop_input.root / "queue.py").write_text("VALUE = 1\nREQUIRED = 100\n", encoding="utf-8")


def test_a_retained_candidate_that_beats_the_input_is_a_success_with_it(tmp_path: Path) -> None:
    loop_input = LoopInput.create(tmp_path)
    agents = _one_candidate(2)

    run = run_request(loop_input.request(), agents)

    assert run.error is None
    assert (run.succeeded, run.status) == (True, RunStatus.COMPLETED)
    assert agents.unscripted == []
    records = CoreRecords(loop_input, run.run_id)
    assert records.outcome == ("terminal", "success")
    assert records.run["result"]["reason"].startswith("adopted: H1")
    assert records.selection is not None
    assert records.selection["kind"] == "retained_candidate"


def test_no_better_candidate_keeps_a_trusted_input_and_still_succeeds(tmp_path: Path) -> None:
    loop_input = LoopInput.create(tmp_path)
    agents = _one_candidate(_FAILS_ACCURACY)

    run = run_request(loop_input.request(), agents)

    assert run.error is None
    assert (run.succeeded, run.status) == (True, RunStatus.COMPLETED)
    assert agents.unscripted == []
    records = CoreRecords(loop_input, run.run_id)
    assert records.strategy["baseline"]["stage"] == "measured"
    assert records.outcome == ("terminal", "success")
    assert records.run["result"]["reason"].startswith("no improvement")
    assert records.selection is not None
    assert records.selection["kind"] == "trusted_baseline"


def test_an_input_that_fails_its_benchmark_with_no_trusted_candidate_is_a_failure(
    tmp_path: Path,
) -> None:
    loop_input = LoopInput.create(tmp_path)
    _failing_input(loop_input)
    agents = _one_candidate(_FAILS_ACCURACY)

    run = run_request(loop_input.request(), agents)

    assert run.error is None
    assert (run.succeeded, run.status) == (False, RunStatus.FAILED)
    assert agents.unscripted == []
    records = CoreRecords(loop_input, run.run_id)
    assert records.strategy["baseline"]["stage"] == "unmeasurable"
    assert records.outcome == ("terminal", "failure")
    assert records.run["result"]["reason"].startswith("no trusted result")
    assert "warmup stopped: 1/100 rounds" in records.run["result"]["reason"]
    assert records.selection is None


@pytest.mark.parametrize("rounds", [1, 2])
def test_an_unmeasurable_input_still_succeeds_when_a_candidate_is_trusted(
    tmp_path: Path, rounds: int
) -> None:
    loop_input = LoopInput.create(tmp_path)
    _failing_input(loop_input)
    agents = ScriptedAgents()
    for number in range(rounds):
        identifier = f"H{number}"
        agents.plan(portfolio(workstream(identifier)))
        agents.implement(identifier, edit_to(number + 2, identifier)).judge(identifier, PASS)

    run = run_request(loop_input.request(max_rounds=rounds), agents)

    assert run.error is None
    assert (run.succeeded, run.status) == (True, RunStatus.COMPLETED)
    records = CoreRecords(loop_input, run.run_id)
    assert records.run["result"]["reason"].startswith("adopted: ")


def test_a_failed_run_reports_a_typed_reason_with_its_counts(tmp_path: Path) -> None:
    loop_input = LoopInput.create(tmp_path)
    _failing_input(loop_input)
    agents = _one_candidate(_FAILS_ACCURACY)

    run = run_request(loop_input.request(), agents)

    assert run.result is not None
    failure = run.result.failure
    assert failure is not None
    assert failure.kind is RunFailureKind.BUDGET_EXHAUSTED
    assert failure.reason.startswith("no trusted result")
    assert failure.workstreams_started == 1
    assert failure.workstream_budget >= failure.workstreams_started
    assert failure.candidates_kept == 0
    published = [event.data for event in run.events if event.type is CoreEventType.RUN_FAILED]
    assert [getattr(data, "failure", None) for data in published] == [failure]


def test_a_successful_run_reports_no_failure(tmp_path: Path) -> None:
    loop_input = LoopInput.create(tmp_path)

    run = run_request(loop_input.request(), _one_candidate(2))

    assert run.result is not None
    assert run.result.failure is None
