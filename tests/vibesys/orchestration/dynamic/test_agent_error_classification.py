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
from tests.vibesys.orchestration.dynamic.test_suspension_shell import (
    _prepared_shell,
    _retry_state,
    _session,
    _submit_failures,
)

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vibesys.orchestration.dynamic.lifecycle import awaiting_evaluation
from vibesys.orchestration.dynamic.models import WaitingForEvaluation
from vibesys.run.dynamic_suspension import EvaluationAttemptBoundError
from vibesys.run.evaluation_backend import SemanticEvaluationStage
from vs_evaluation.api import EvaluationAgentAccessError, EvaluationState, FailureKind
from vs_evaluation.api.testing import FakeEvaluationSettlements
from vs_runtime.api import StructuredResponseError

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.api.testing import FakeRun


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

    async def scenario() -> FakeRun:
        run = baseline_run(tmp_path, script)
        run.evaluation.submitted_revisions["eval_bad"] = "measured-revision"
        run.evaluation.script_benchmark(INPUT_BASELINE, throughput(10.0))
        await PLUGIN.orchestrate(
            run, dynamic_options(judge_every=1, max_in_flight=1, max_retries_per_round=3)
        )
        return run

    run = asyncio.run(scenario())
    state = run.state.commits[-1].value.model_dump(mode="json")
    assert state["workstreams"][0]["implementation"]["summary"] == "Implemented retry."
    assert all(intent["stage"] != "blocked" for intent in state["lifecycle"]["intents"].values())
    error_type = EvaluationAgentAccessError if kind == "access" else StructuredResponseError
    assert error_type.__name__ in caplog.text
    assert ("bad-handle" if kind == "access" else "wrong-revision") in caplog.text


@pytest.mark.asyncio
async def test_rejected_resumed_wait_ends_known_attempt_and_preserves_peer(tmp_path: Path) -> None:
    rejected: dict[str, object] = {
        "kind": "waiting_for_evaluation",
        "handles": ["foreign-profiler-operation"],
    }
    run = baseline_run(tmp_path, Script({IMPLEMENTER.id: [rejected, rejected]}))
    revision = await run.workspaces.root.snapshot("root")
    workspace = await run.workspaces.create_candidate(revision, member_id="held")
    peer = await run.workspaces.create_candidate(revision, member_id="peer")
    handles = await _submit_failures(run.evaluation, workspace, revision, FailureKind.TRACEBACK, 0)
    settlements = run.evaluation.settlement_observations
    assert isinstance(settlements, FakeEvaluationSettlements)
    report = await settlements.coordinator.recorded_snapshot(handles[0])
    peer_handle = await settlements.submit(
        report.request.model_copy(update={"key": "peer", "owner_scope": peer.id}),
        SemanticEvaluationStage.model_validate(report.request.stages[0].payload).fingerprints,
    )
    calls = []
    session, client = await _session(run, tmp_path, rejected, calls.append, workspace)
    operation = "held/implementer/1"
    state = _retry_state(operation, revision)

    async def commit(label: str) -> None:
        await run.state.commit(state, label=label)

    shell = _prepared_shell(run, state, commit, operation, workspace)
    try:
        await shell.yield_turn(
            0,
            workspace,
            session,
            WaitingForEvaluation(kind="waiting_for_evaluation", handles=tuple(handles)),
        )
        with pytest.raises(EvaluationAttemptBoundError, match="foreign-profiler-operation"):
            await shell.run_wait(0, workspace, session)
        snapshot = state.model_dump(mode="json")
        assert snapshot["workstreams"][0]["phase"] == "failed"
        assert not awaiting_evaluation(state.lifecycle, "held", 1)
        assert all(
            intent["stage"] != "blocked" for intent in snapshot["lifecycle"]["intents"].values()
        )
        assert all(
            intent["stage"] == "completed"
            for intent in snapshot["lifecycle"]["intents"].values()
            if intent["kind"] == "resume"
        )
        assert await settlements.coordinator.status(peer_handle) is EvaluationState.QUEUED
        assert peer_handle not in run.evaluation.cancelled_submissions
    finally:
        await session.close()
        await workspace.discard()
        await peer.discard()
        client.close()
