"""A workstream's trusted benchmark/accuracy gates carry its round label.

`Workstreams._assess` threads its round label (the attempt's sequence number)
through to `Run.evaluation.accuracy`/`.benchmark`, so the gate events those
calls emit can be correlated with the workstream whose candidate they judged.
`FakeEvaluation` (see `vs_runtime.api.testing`) records every call it receives;
these tests drive the real plugin end to end through `PLUGIN.orchestrate` and
inspect what reached it, instead of reaching into `Workstreams`' private
methods.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from tests.vibesys.orchestration.dynamic._support import (
    Script,
    baseline_run,
    dynamic_options,
    implementation,
    portfolio,
)

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR

if TYPE_CHECKING:
    from pathlib import Path


def test_benchmark_gate_carries_the_evaluating_workstream_round_label(tmp_path: Path) -> None:
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("cache")],
            IMPLEMENTER.id: [implementation("cache")],
            JUDGE.id: [{"passed": True, "analysis": "Candidate is correct."}],
        }
    )
    fake = baseline_run(tmp_path, script)
    asyncio.run(
        PLUGIN.orchestrate(
            fake,
            dynamic_options(max_in_flight=1, max_rounds=1, max_retries_per_round=1, judge_every=1),
        )
    )
    # `baseline_run` configures a benchmark but no accuracy command, so a
    # review-passed nominated candidate is evaluated by benchmark alone.
    [call] = [call for call in fake.evaluation.benchmark_calls if call.workspace.id is not None]
    assert call.round_label == "round-1"
