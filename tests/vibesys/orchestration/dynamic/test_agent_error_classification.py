"""Known agent mistakes and unknown provider outcomes have distinct durable semantics."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.dynamic._support import (
    INPUT_BASELINE,
    Script,
    baseline_run,
    dynamic_options,
    implementation,
    portfolio,
    throughput,
)

from vibesys.orchestration.dynamic import PLUGIN, DynamicState
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vibesys.orchestration.dynamic.lifecycle import IntentStage
from vs_evaluation.api import EvaluationAgentAccessError
from vs_runtime.api import StructuredResponseError

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("kind", ["access", "structured"])
def test_known_agent_error_logs_cause_and_allows_next_attempt(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, kind: str
) -> None:
    rejected: object = (
        {"kind": "waiting_for_evaluation", "handles": ["bad-handle"]}
        if kind == "access"
        else {
            "summary": "Incorrect attribution.",
            "outcome": "nominated",
            "evidence": [
                {"location": "eval_bad", "purpose": "measurement", "revision": "wrong-revision"}
            ],
        }
    )
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("retry")],
            IMPLEMENTER.id: [rejected, rejected, implementation("retry")],
            JUDGE.id: [{"passed": True, "analysis": "Corrected."}],
        }
    )

    async def scenario() -> DynamicState | None:
        run = baseline_run(tmp_path, script)
        run.evaluation.submitted_revisions["eval_bad"] = "measured-revision"
        run.evaluation.script_benchmark(INPUT_BASELINE, throughput(10.0))
        await PLUGIN.orchestrate(
            run, dynamic_options(judge_every=1, max_in_flight=1, max_retries_per_round=3)
        )
        return await run.state.load(DynamicState)

    state = asyncio.run(scenario())
    assert state is not None
    assert state.workstreams[0].implementation is not None
    assert state.workstreams[0].implementation.summary == "Implemented retry."
    assert all(
        intent.stage is not IntentStage.BLOCKED for intent in state.lifecycle.intents.values()
    )
    error_type = EvaluationAgentAccessError if kind == "access" else StructuredResponseError
    assert error_type.__name__ in caplog.text
    assert ("bad-handle" if kind == "access" else "wrong-revision") in caplog.text
