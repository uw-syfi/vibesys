"""How a dynamic run ends: the exit status an operator and a script can rely on.

Success means the operator has a trusted, measured result to use. A run ends as
``adopted`` (a retained, verified candidate beat the input), ``no improvement``
(none did, but the input was measured and trusted) or ``no trusted result`` (the
input failed its measurement and no trusted candidate exists). The first is asserted by
the whole-run scenario in ``test_loop`` (an adopted hypothesis), the last by its
failed-benchmark scenario and by ``test_input_measurement`` (an unmeasurable input
that a candidate still beats); this module holds the one that needs its own run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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

from vibesys.api import RunStatus

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


def test_no_better_candidate_keeps_a_trusted_input_and_still_succeeds(tmp_path: Path) -> None:
    loop_input = LoopInput.create(tmp_path)
    agents = _one_candidate(_FAILS_ACCURACY)

    run = run_request(
        loop_input.request(),
        agents,
        slurm_process=loop_input.connector,
        state_stores=loop_input.state_stores,
    )

    assert run.error is None
    assert (run.succeeded, run.status) == (True, RunStatus.COMPLETED)
    assert agents.unscripted == []
    records = CoreRecords(loop_input, run.run_id)
    assert records.strategy["baseline"]["stage"] == "measured"
    assert records.outcome == ("terminal", "success")
    assert records.run["result"]["reason"].startswith("no improvement")
    assert records.selection is not None
    assert records.selection["kind"] == "trusted_baseline"
