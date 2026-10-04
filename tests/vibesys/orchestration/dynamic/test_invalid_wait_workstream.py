"""Invalid wait replies continue live workstreams through the public plugin API."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.dynamic._support import (
    Script,
    baseline_run,
    dynamic_options,
    implementation,
    portfolio,
)

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vs_evaluation.api import (
    ContentDigest,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvidenceFingerprints,
)
from vs_evaluation.api.testing import FakeEvaluationSettlements
from vs_runtime.api import RunStatus

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

    from vs_runtime.api import AgentRole


@pytest.mark.parametrize("role", [IMPLEMENTER, JUDGE], ids=lambda role: role.id)
@pytest.mark.parametrize(
    "kind", ["foreign", "own_profiler", "foreign_profiler", "unknown", "malformed"]
)
def test_invalid_wait_preserves_live_workstream(role: AgentRole, kind: str, tmp_path: Path) -> None:
    """The bad final handle is corrected without a dispatch fence or lost pending capture."""

    async def scenario() -> None:
        settlements = FakeEvaluationSettlements()
        script = Script({ORCHESTRATOR.id: [portfolio("held")]})
        seen_bad = False
        corrected = False
        owned: str | None = None
        foreign: str | None = None
        digest = ContentDigest.sha256(b"snapshot")
        fingerprints = EvidenceFingerprints(
            candidate=digest, evaluator=digest, workload=digest, environment=digest
        )

        async def respond(
            current: AgentRole,
            _history: tuple[str, ...],
            message: str,
            response: type[BaseModel] | None,
        ) -> object:
            nonlocal seen_bad, corrected, owned, foreign
            if current.id == role.id and not seen_bad:
                workspace = run.workspaces.candidates[-1]
                payload = {
                    "snapshot": await workspace.snapshot("agent-evaluation"),
                    "kind": "benchmark",
                    "fingerprints": fingerprints.model_dump(mode="json"),
                }
                owned = await settlements.submit(
                    EvaluationRequest(
                        key="owned",
                        owner_scope=workspace.id,
                        stages=(EvaluationStep(name="benchmark", payload=payload),),
                    ),
                    fingerprints,
                )
                foreign = await settlements.submit(
                    EvaluationRequest(
                        key="foreign",
                        owner_scope="foreign-scope",
                        stages=(EvaluationStep(name="benchmark", payload=payload),),
                    ),
                    fingerprints,
                )
                seen_bad = True
                handle = {
                    "foreign": foreign,
                    "own_profiler": "a" * 32,
                    "foreign_profiler": "b" * 32,
                    "unknown": "eval_unknown",
                    "malformed": " bad ",
                }[kind]
                return {"kind": "waiting_for_evaluation", "handles": [handle]}
            if current.id == role.id and seen_bad:
                assert owned is not None
                assert await settlements.coordinator.status(owned) is EvaluationState.QUEUED
                corrected = True
            if current.id == IMPLEMENTER.id:
                return implementation("held")
            if current.id == JUDGE.id:
                return {"passed": True, "analysis": "Candidate checked."}
            return script.respond(current, _history, message, response)

        base = baseline_run(tmp_path, script, responder=respond)
        base.evaluation.settlement_observations = settlements
        run = base
        status = await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1))
        assert status is RunStatus.SUCCEEDED
        assert corrected
        state = run.state.commits[-1].value.model_dump(mode="json")
        assert state["workstreams"][0]["sequence"] == 1
        assert all(
            intent["stage"] != "blocked"
            for intent in state["lifecycle"]["intents"].values()
            if intent["kind"] == "turn"
        )

    asyncio.run(scenario())
