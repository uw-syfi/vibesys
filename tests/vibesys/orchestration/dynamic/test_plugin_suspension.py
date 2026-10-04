"""Suspension activation and recovery through the existing dynamic plugin API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Literal

import pytest
from tests.vibesys.orchestration.dynamic._support import (
    Script,
    baseline_run,
    dynamic_options,
    portfolio,
)

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, ORCHESTRATOR
from vibesys.orchestration.dynamic.lifecycle import IntentKind, IntentStage
from vibesys.orchestration.dynamic.models import (
    DurableStateCommitError,
    DynamicState,
)
from vs_agent.api import (
    AgentClient,
    AgentExecutionPolicy,
    AgentSessionKey,
    AgentSessionSpec,
    AgentTurnRequest,
    SessionScope,
)
from vs_agent.api.testing import FakeAgentSessions, FakeDriver
from vs_evaluation.api import (
    ArtifactDigest,
    ContentDigest,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvaluationStepResult,
    EvidenceFingerprints,
    EvidenceKind,
    EvidenceOutcome,
    StageState,
    TrustedEvidence,
)
from vs_evaluation.api.testing import FakeEvaluationSettlements
from vs_runtime.api import Run, RunStopped, RuntimeContractError
from vs_runtime.api.infrastructure import create_run_control_channel, stop_gated_evaluation
from vs_runtime.api.testing import FakeEvaluation, FakeRunControlEventSink

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pydantic import BaseModel

    from vs_runtime.api import AgentRole, RunStatus
    from vs_runtime.api.infrastructure import RunControlChannel
    from vs_runtime.api.testing import FakeRun


class _WaitingEvaluation(FakeEvaluation):
    """Keep additive capture configuration available against the old slotted Fake."""


@dataclass
class _Scenario:
    run: FakeRun
    runtime: Run
    channel: RunControlChannel
    evaluations: FakeEvaluationSettlements
    evaluation: _WaitingEvaluation
    handle: str
    calls: list[AgentTurnRequest]
    client: AgentClient

    def start(self) -> asyncio.Future[RunStatus]:
        return asyncio.ensure_future(
            PLUGIN.orchestrate(self.runtime, dynamic_options(max_in_flight=1))
        )

    async def waiting(self, task: asyncio.Future[RunStatus]) -> DynamicState:
        barrier = asyncio.create_task(self.evaluations.executor.wait_started.wait())
        done, _ = await asyncio.wait({task, barrier}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            barrier.cancel()
            await task
        state = await self.run.state.load(DynamicState)
        assert state is not None
        return state

    async def complete(
        self, corruption: Literal["owner", "fingerprints", "malformed"] | None = None
    ) -> None:
        digest = ContentDigest.sha256(b"immutable capture")
        evidence = TrustedEvidence(
            evidence_id="a" * 64,
            evaluation_id=self.handle,
            stage_name="benchmark",
            kind=EvidenceKind.BENCHMARK,
            outcome=EvidenceOutcome.OBSERVED,
            fingerprints=EvidenceFingerprints(
                candidate=digest,
                evaluator=digest,
                workload=digest,
                environment=digest,
            ),
            trusted_inputs=digest,
            accepted_round=0,
            artifacts=(ArtifactDigest(path="measurement.json", digest=digest),),
        )
        self.evaluations.executor.set_state(
            self.handle,
            EvaluationState.SUCCEEDED,
            stage_results=(
                EvaluationStepResult(
                    name="benchmark",
                    state=StageState.SUCCEEDED,
                    result=evidence.model_dump(mode="json"),
                ),
            ),
        )
        await self.evaluations.coordinator.status(self.handle)
        report = await self.evaluations.coordinator.recorded_snapshot(self.handle)
        if corruption == "owner":
            report = report.model_copy(
                update={"request": report.request.model_copy(update={"owner_generation": 1})}
            )
        if corruption == "fingerprints":
            wrong = evidence.model_copy(
                update={
                    "fingerprints": evidence.fingerprints.model_copy(
                        update={"candidate": ContentDigest.sha256(b"different capture")}
                    )
                }
            )
            report = report.model_copy(
                update={
                    "stage_results": (
                        report.stage_results[0].model_copy(
                            update={"result": wrong.model_dump(mode="json")}
                        ),
                    )
                }
            )
        self.evaluation.submitted_reports[self.handle] = (
            "{}" if corruption == "malformed" else report.model_dump_json()
        )


async def _open(
    tmp_path: Path,
    on_resume: Callable[[AgentTurnRequest], None] | None = None,
) -> _Scenario:
    script = Script({ORCHESTRATOR.id: [portfolio("held")]})
    handles: list[str] = []

    def respond(
        role: AgentRole, history: tuple[str, ...], message: str, response: type[BaseModel] | None
    ) -> object:
        if role.id == IMPLEMENTER.id:
            return {"kind": "waiting_for_evaluation", "handles": handles}
        return script.respond(role, history, message, response)

    run = baseline_run(tmp_path, script, responder=respond)
    evaluation = _WaitingEvaluation(
        **{
            item.name: getattr(run.evaluation, item.name)
            for item in fields(FakeEvaluation)
            if item.init
        }
    )
    evaluation.submitted_reports = {}
    root = await run.workspaces.root.snapshot("root")
    prototype = await run.workspaces.create_candidate(root, member_id="held")
    await prototype.discard()
    evaluations = FakeEvaluationSettlements()
    digest = ContentDigest.sha256(b"immutable capture")
    handle = await evaluations.submit(
        EvaluationRequest(
            key="plugin-held",
            owner_scope=prototype.id,
            owner_generation=0,
            stages=(EvaluationStep(name="benchmark", payload={}),),
        ),
        EvidenceFingerprints(
            candidate=digest, evaluator=digest, workload=digest, environment=digest
        ),
    )
    handles.append(handle)
    evaluation.settlement_observations = evaluations
    evaluation.submitted_revisions = {handle: root}
    evaluation.submitted_generations = {handle: 0}
    evaluation.accepted_evidence = {handle: ("a" * 64,)}
    calls: list[AgentTurnRequest] = []

    def turn(request: AgentTurnRequest) -> None:
        calls.append(request)
        if request.invocation_id is not None and on_resume is not None:
            on_resume(request)

    client = AgentClient(
        FakeDriver(
            answer={
                "summary": "Awaited result checked.",
                "outcome": "continue",
                "next_step": "Complete.",
            },
            on_turn=turn,
        )
    )
    key = AgentSessionKey(SessionScope.MEMBER, f"{IMPLEMENTER.id}:held")
    spec = AgentSessionSpec(
        role=IMPLEMENTER.id,
        provider="fake",
        workspace=tmp_path,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    client.run(session_spec=spec, turn=AgentTurnRequest(message="initial"), session_key=key)
    transport = FakeAgentSessions(client)
    transport.bind(key, spec, AgentTurnRequest(message="resume"))
    run.agents.bind_session_transport(transport)
    channel = create_run_control_channel(FakeRunControlEventSink())
    runtime = Run(
        run_id=run.run_id,
        facts=run.facts,
        agents=run.agents,
        workspaces=run.workspaces,
        evaluation=stop_gated_evaluation(evaluation, channel),
        state=run.state,
        control=run.control,
        commands=run.commands,
        skills=run.skills,
        observations=run.observations,
    )
    evaluations.executor.wait_started.clear()
    return _Scenario(run, runtime, channel, evaluations, evaluation, handle, calls, client)


@pytest.mark.parametrize("boundary", ["live", "waiting", "result", "acknowledged"])
def test_retains_waiting_implementation_and_resumes_once(
    tmp_path: Path,
    boundary: Literal["live", "waiting", "result", "acknowledged"],
) -> None:
    """Restart reuses both the charged turn and the durable accepted resume result."""

    async def scenario() -> None:
        opened = await _open(tmp_path)
        task = opened.start()
        parked = await opened.waiting(task)
        assert parked.workstreams[0].budget.spent == 1
        retained = next(iter(parked.lifecycle.continuations.values())).retained_revision
        assert opened.run.workspaces.root.knows_revision(retained)
        if boundary == "waiting":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            task = opened.start()
        if boundary in {"result", "acknowledged"}:
            label = (
                "dynamic: held implemented"
                if boundary == "result"
                else "dynamic: record hypothesis held"
            )
            opened.run.state.script_commit_at(label, RuntimeError("simulated crash"))
        opened.evaluations.executor.clock.advance(420)
        assert len(opened.calls) == 1
        await opened.complete()
        if boundary in {"result", "acknowledged"}:
            with pytest.raises(DurableStateCommitError):
                await task
            task = opened.start()
        await task
        final = await opened.run.state.load(DynamicState)
        assert final is not None
        assert final.workstreams[0].budget.spent == 1
        assert len(final.search.rounds) == 1
        assert len(opened.calls) == 2
        assert "measurement.json" in opened.calls[-1].message
        resumes = [
            intent
            for intent in final.lifecycle.intents.values()
            if intent.kind is IntentKind.RESUME
        ]
        assert len(resumes) == 1
        assert resumes[0].stage is IntentStage.COMPLETED
        opened.client.close()

    asyncio.run(scenario())


def test_stop_during_suspension_preserves_budget_and_continuation(tmp_path: Path) -> None:
    async def scenario() -> None:
        opened = await _open(tmp_path)
        task = opened.start()
        parked = await opened.waiting(task)
        opened.channel.request_stop()
        with pytest.raises(RunStopped):
            await task
        stopped = await opened.run.state.load(DynamicState)
        assert stopped is not None
        assert stopped.lifecycle.stopped
        assert stopped.workstreams[0].budget == parked.workstreams[0].budget
        assert stopped.search.rounds == []
        assert len(opened.calls) == 1
        assert opened.evaluations.executor.cancellations == []
        opened.channel.resume()
        await opened.complete()
        await opened.start()
        assert len(opened.calls) == 2
        opened.client.close()

    asyncio.run(scenario())


def test_ambiguous_resume_is_blocked_without_retry_or_scientific_settlement(tmp_path: Path) -> None:
    def lose_acknowledgement(_request: AgentTurnRequest) -> None:
        message = "provider acknowledgement lost after acceptance"
        raise OSError(message)

    async def scenario() -> None:
        opened = await _open(tmp_path, lose_acknowledgement)
        task = opened.start()
        parked = await opened.waiting(task)
        await opened.complete()
        with pytest.raises(RuntimeContractError, match="reconciliation"):
            await task
        blocked = await opened.run.state.load(DynamicState)
        assert blocked is not None
        assert blocked.workstreams[0].budget == parked.workstreams[0].budget
        assert blocked.search.rounds == []
        assert any(
            intent.kind is IntentKind.RESUME and intent.stage is IntentStage.BLOCKED
            for intent in blocked.lifecycle.intents.values()
        )
        with pytest.raises(RuntimeContractError, match="reconciliation"):
            await opened.start()
        assert len(opened.calls) == 2
        opened.client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("corruption", ["owner", "fingerprints", "malformed"])
def test_resume_rejects_misattributed_or_invalid_trusted_report_without_charging(
    tmp_path: Path, corruption: Literal["owner", "fingerprints", "malformed"]
) -> None:
    async def scenario() -> None:
        opened = await _open(tmp_path)
        task = opened.start()
        waiting = await opened.waiting(task)
        await opened.complete(corruption)
        with pytest.raises(RuntimeContractError):
            await task
        unresolved = await opened.run.state.load(DynamicState)
        assert unresolved is not None
        assert unresolved.workstreams[0].budget == waiting.workstreams[0].budget
        assert unresolved.search.rounds == []
        assert len(opened.calls) == 1
        opened.client.close()

    asyncio.run(scenario())
