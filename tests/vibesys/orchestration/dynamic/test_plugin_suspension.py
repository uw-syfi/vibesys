"""Suspension activation and recovery through the existing dynamic plugin API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, fields
from itertools import permutations
from typing import TYPE_CHECKING, Literal

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
from vibesys.orchestration.dynamic.lifecycle import IntentKind, IntentStage
from vibesys.orchestration.dynamic.models import (
    DurableStateCommitError,
    DynamicState,
)
from vibesys.run.evaluation_backend import SemanticEvaluationStage
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
    StoredEvaluation,
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

    from vs_agent.api import InvocationOutcome
    from vs_prompts.api import RenderedPrompt
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
    judge_every: int = 100
    extra_handles: tuple[str, ...] = ()
    independent_peer: bool = False

    def start(self) -> asyncio.Future[RunStatus]:
        return asyncio.ensure_future(
            PLUGIN.orchestrate(
                self.runtime,
                dynamic_options(
                    max_in_flight=1,
                    max_rounds=2 if self.independent_peer else 1,
                    judge_every=self.judge_every,
                ),
            )
        )

    async def waiting(self, task: asyncio.Future[RunStatus]) -> DynamicState:
        barrier = asyncio.create_task(self.evaluations.executor.wait_started.wait())
        done, _ = await asyncio.wait({task, barrier}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            barrier.cancel()
            await task
        state = await self.run.state.load(DynamicState)
        assert state is not None
        assert state.lifecycle.continuations
        return state

    async def complete(
        self,
        corruption: Literal["owner", "fingerprints", "malformed"] | None = None,
        *,
        handle: str | None = None,
    ) -> None:
        handle = handle or self.handle
        digest = ContentDigest.sha256(b"immutable capture")
        evidence = TrustedEvidence(
            evidence_id="a" * 64,
            evaluation_id=handle,
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
            handle,
            EvaluationState.SUCCEEDED,
            stage_results=(
                EvaluationStepResult(
                    name="benchmark",
                    state=StageState.SUCCEEDED,
                    result=evidence.model_dump(mode="json"),
                ),
            ),
        )
        await self.evaluations.coordinator.status(handle)
        report = await self.evaluations.coordinator.recorded_snapshot(handle)
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
        self.evaluation.submitted_reports[handle] = (
            "{}" if corruption == "malformed" else report.model_dump_json()
        )


async def _open(
    tmp_path: Path,
    on_resume: Callable[[AgentTurnRequest], None] | None = None,
    *,
    malformed_resume: bool = False,
    waiting_role: Literal["implementer", "judge"] = "implementer",
    submission: int | StoredEvaluation | Literal["independent_peer", "continue_failed"] = 1,
) -> _Scenario:
    handle_count = submission if isinstance(submission, int) else 1
    produced_report = submission if isinstance(submission, StoredEvaluation) else None
    independent_peer = submission in ("independent_peer", "continue_failed")
    continue_failed = submission == "continue_failed"
    requested_wait = False
    script = Script(
        {
            ORCHESTRATOR.id: [
                portfolio("held"),
                portfolio("held", continue_hypothesis=True)
                if continue_failed
                else portfolio("healthy"),
            ],
            IMPLEMENTER.id: [implementation("healthy")],
            JUDGE.id: [{"passed": True, "analysis": "Independent candidate is correct."}],
        }
    )
    handles: list[str] = []

    def respond(
        role: AgentRole, history: tuple[str, ...], message: str, response: type[BaseModel] | None
    ) -> object:
        nonlocal requested_wait
        if continue_failed and role.id == IMPLEMENTER.id and requested_wait:
            return implementation("held-fixed")
        if role.id == IMPLEMENTER.id and message.startswith("Own hypothesis `healthy`"):
            return script.respond(role, history, message, response)
        if role.id == IMPLEMENTER.id and waiting_role == "judge":
            return implementation("held")
        if role.id == (IMPLEMENTER.id if waiting_role == "implementer" else JUDGE.id):
            requested_wait = True
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
    evaluation.submitted_deadlines = {}
    root = await run.workspaces.root.snapshot("root")
    prototype = await run.workspaces.create_candidate(root, member_id="held")
    await prototype.discard()
    evaluations = FakeEvaluationSettlements()
    digest = ContentDigest.sha256(b"immutable capture")
    captured_stage = SemanticEvaluationStage(
        snapshot=root,
        kind=EvidenceKind.BENCHMARK,
        fingerprints=EvidenceFingerprints(
            candidate=digest, evaluator=digest, workload=digest, environment=digest
        ),
    ).model_dump(mode="json")
    handle = await evaluations.submit(
        produced_report.request
        if produced_report is not None
        else EvaluationRequest(
            key="plugin-held",
            owner_scope=prototype.id,
            owner_generation=0,
            stages=(EvaluationStep(name="benchmark", payload=captured_stage),),
        ),
        TrustedEvidence.model_validate(produced_report.stage_results[0].result).fingerprints
        if produced_report is not None
        else EvidenceFingerprints(
            candidate=digest, evaluator=digest, workload=digest, environment=digest
        ),
    )
    handles.append(handle)
    handles.extend(
        [
            await evaluations.submit(
                EvaluationRequest(
                    key=f"plugin-held-{index}",
                    owner_scope=prototype.id,
                    owner_generation=0,
                    stages=(EvaluationStep(name="benchmark", payload=captured_stage),),
                ),
                EvidenceFingerprints(
                    candidate=digest, evaluator=digest, workload=digest, environment=digest
                ),
            )
            for index in range(1, handle_count)
        ]
    )
    evaluation.settlement_observations = evaluations
    evaluation.submitted_revisions = dict.fromkeys(handles, root)
    evaluation.submitted_generations = dict.fromkeys(handles, 0)
    evaluation.submitted_deadlines = dict.fromkeys(handles, 1000.0)
    evaluation.accepted_evidence = dict.fromkeys(handles, ("a" * 64,))
    calls: list[AgentTurnRequest] = []

    def turn(request: AgentTurnRequest) -> None:
        calls.append(request)
        if request.invocation_id is not None and on_resume is not None:
            on_resume(request)

    client = AgentClient(
        FakeDriver(
            answer={"unexpected": True}
            if malformed_resume
            else {"passed": True, "analysis": "Trusted result checked."}
            if waiting_role == "judge"
            else {
                "summary": "Awaited result checked.",
                "outcome": "continue",
                "next_step": "Complete.",
            },
            on_turn=turn,
        )
    )
    transport = FakeAgentSessions(client)
    # Every initial role journals against its exact configured provider session.
    # Judge suspension runs an implementer turn before entering the review.
    initial_roles = (IMPLEMENTER, JUDGE) if waiting_role == "judge" else (IMPLEMENTER,)
    for role in initial_roles:
        key = AgentSessionKey(SessionScope.MEMBER, f"{role.id}:held")
        spec = AgentSessionSpec(
            role=role.id,
            provider="fake",
            workspace=tmp_path,
            policy=AgentExecutionPolicy(require_enforcement=False),
        )
        client.run(session_spec=spec, turn=AgentTurnRequest(message="initial"), session_key=key)
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
    return _Scenario(
        run,
        runtime,
        channel,
        evaluations,
        evaluation,
        handle,
        calls,
        client,
        judge_every=1 if waiting_role == "judge" else 100,
        extra_handles=tuple(handles[1:]),
        independent_peer=independent_peer,
    )


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


def test_ambiguous_resume_ends_attempt_without_retry_and_run_continues(tmp_path: Path) -> None:
    def lose_acknowledgement(_request: AgentTurnRequest) -> None:
        message = "provider acknowledgement lost after acceptance"
        raise OSError(message)

    async def scenario() -> None:
        opened = await _open(tmp_path, lose_acknowledgement)
        task = opened.start()
        parked = await opened.waiting(task)
        await opened.complete()
        await task
        blocked = await opened.run.state.load(DynamicState)
        assert blocked is not None
        assert blocked.workstreams[0].budget.spent >= parked.workstreams[0].budget.spent
        assert "reconciliation" in (blocked.workstreams[0].last_error or "")
        assert len(blocked.search.rounds) == 1
        assert not blocked.search.rounds[0].passed
        assert blocked.search.rounds[0].hypothesis_outcome == "implementation_failed"
        assert any(
            intent.kind is IntentKind.RESUME and intent.stage is IntentStage.BLOCKED
            for intent in blocked.lifecycle.intents.values()
        )
        await opened.start()
        assert len(opened.calls) == 2
        opened.client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("corruption", ["owner", "fingerprints"])
def test_resume_rejects_misattributed_or_invalid_trusted_report_without_charging(
    tmp_path: Path, corruption: Literal["owner", "fingerprints"]
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


def test_deadline_interrupts_host_wait_and_resumes_once_with_trusted_timeout(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        opened = await _open(tmp_path)
        task = opened.start()
        waiting = await opened.waiting(task)
        assert waiting.lifecycle.continuations
        await opened.evaluation.deadline_wait_started.wait()
        opened.evaluation.advance_time(1000.0)
        await task
        settled = await opened.run.state.load(DynamicState)
        assert settled is not None
        continuation = next(iter(settled.lifecycle.continuations.values()))
        assert continuation.timed_out is not None
        assert continuation.settlements == {}
        assert opened.evaluation.cancelled_submissions == [opened.handle]
        assert settled.workstreams[0].budget == waiting.workstreams[0].budget
        assert len(opened.calls) == 2
        assert "timed_out" in opened.calls[-1].message
        assert continuation.timed_out.reached_at_s == 1000.0
        opened.client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("waiting_role", ["implementer", "judge"])
def test_malformed_completed_resume_ends_attempt_and_run_continues(
    tmp_path: Path, waiting_role: Literal["implementer", "judge"]
) -> None:
    async def scenario() -> None:
        opened = await _open(tmp_path, malformed_resume=True, waiting_role=waiting_role)
        task = opened.start()
        waiting = await opened.waiting(task)
        await opened.complete()
        await task
        blocked = await opened.run.state.load(DynamicState)
        assert blocked is not None
        assert blocked.workstreams[0].budget.spent >= waiting.workstreams[0].budget.spent
        assert blocked.workstreams[0].last_error
        assert len(blocked.search.rounds) == 1
        assert not blocked.search.rounds[0].passed
        assert blocked.search.rounds[0].hypothesis_outcome == "implementation_failed"
        assert any(
            intent.kind is IntentKind.RESUME and intent.stage is IntentStage.BLOCKED
            for intent in blocked.lifecycle.intents.values()
        )
        assert len(opened.calls) == 2
        await opened.start()
        assert len(opened.calls) == 2
        opened.client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("restart", [False, True])
def test_judge_suspension_resumes_same_review_session_without_reimplementation(
    tmp_path: Path, *, restart: bool
) -> None:
    async def scenario() -> None:
        opened = await _open(tmp_path, waiting_role="judge")
        task = opened.start()
        waiting = await opened.waiting(task)
        continuation = next(iter(waiting.lifecycle.continuations.values()))
        assert continuation.role == "judge"
        assert continuation.original_stage == "implemented"
        if restart:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            task = opened.start()
        opened.evaluation.advance_time(420.0)
        assert len(opened.calls) == 2
        await opened.complete()
        await task
        final = await opened.run.state.load(DynamicState)
        assert final is not None
        assert final.workstreams[0].budget == waiting.workstreams[0].budget
        assert final.workstreams[0].review is not None
        assert len(opened.calls) == 3
        key = AgentSessionKey(SessionScope.MEMBER, f"{JUDGE.id}:held")
        assert continuation.session_key == str(key)
        assert opened.calls[-1].expected_provider_session_id == opened.client.provider_session_id(
            key
        )
        assert (
            final.workstreams[0].invocation_sequence == waiting.workstreams[0].invocation_sequence
        )
        opened.client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("order", list(permutations(range(3))))
def test_reordered_and_duplicate_handle_completions_resume_only_after_wait_all(
    tmp_path: Path, order: tuple[int, ...]
) -> None:
    async def scenario() -> None:
        opened = await _open(tmp_path, submission=3)
        task = opened.start()
        waiting = await opened.waiting(task)
        handles = (opened.handle, *opened.extra_handles)
        for index in order[:-1]:
            opened.evaluations.executor.wait_started.clear()
            await opened.complete(handle=handles[index])
            await opened.evaluations.executor.wait_started.wait()
            state = await opened.run.state.load(DynamicState)
            assert state is not None
            assert handles[index] in next(iter(state.lifecycle.continuations.values())).settlements
            assert state.workstreams[0].budget == waiting.workstreams[0].budget
            assert len(opened.calls) == 1
            await opened.complete(handle=handles[index])
            assert len(opened.calls) == 1
        await opened.complete(handle=handles[order[-1]])
        await task
        settled = await opened.run.state.load(DynamicState)
        assert settled is not None
        continuation = next(iter(settled.lifecycle.continuations.values()))
        assert set(continuation.settlements) == set(handles)
        assert settled.workstreams[0].budget == waiting.workstreams[0].budget
        assert len(opened.calls) == 2
        assert len(settled.search.rounds) == 1
        opened.client.close()

    asyncio.run(scenario())


def test_missing_declared_deadline_ends_attempt_without_repeating_yield(tmp_path: Path) -> None:
    async def scenario() -> None:
        opened = await _open(tmp_path)
        opened.evaluation.submitted_deadlines.clear()
        await opened.start()
        blocked = await opened.run.state.load(DynamicState)
        assert blocked is not None
        assert blocked.workstreams[0].phase.value == "failed"
        assert "no submitted deadline" in (blocked.workstreams[0].last_error or "")
        assert len(blocked.search.rounds) == 1
        assert not blocked.search.rounds[0].passed
        assert blocked.search.rounds[0].hypothesis_outcome == "implementation_failed"
        assert blocked.lifecycle.continuations == {}
        assert any(
            intent.kind is IntentKind.TURN and intent.stage is IntentStage.BLOCKED
            for intent in blocked.lifecycle.intents.values()
        )
        before = sum(len(session.history) for session in opened.run.agents.sessions)
        await opened.start()
        assert sum(len(session.history) for session in opened.run.agents.sessions) == before
        assert len(opened.calls) == 1
        opened.client.close()

    asyncio.run(scenario())


def test_malformed_evaluation_report_ends_attempt_before_provider_resume(tmp_path: Path) -> None:
    async def scenario() -> None:
        opened = await _open(tmp_path)
        task = opened.start()
        await opened.waiting(task)
        await opened.complete("malformed")
        await task
        final = await opened.run.state.load(DynamicState)
        assert final is not None
        assert len(final.search.rounds) == 1
        assert not final.search.rounds[0].passed
        assert final.search.rounds[0].hypothesis_outcome == "implementation_failed"
        assert final.workstreams[0].last_error
        assert len(opened.calls) == 1
        await opened.start()
        assert len(opened.calls) == 1
        opened.client.close()

    asyncio.run(scenario())


class _BrokenResumeSessions(FakeAgentSessions):
    """A provider adapter fault before dispatch, with ordinary session identity."""

    def resume(
        self, key: AgentSessionKey, message: RenderedPrompt, invocation_id: str
    ) -> InvocationOutcome:
        del key, message, invocation_id
        detail = "unexpected adapter failure before resume dispatch"
        raise RuntimeError(detail)


def test_unexpected_resume_fault_ends_one_attempt_and_persists_failure(tmp_path: Path) -> None:
    async def scenario() -> None:
        opened = await _open(tmp_path, submission="independent_peer")
        key = AgentSessionKey(SessionScope.MEMBER, f"{IMPLEMENTER.id}:held")
        transport = _BrokenResumeSessions(opened.client)
        transport.bind(
            key,
            AgentSessionSpec(
                role=IMPLEMENTER.id,
                provider="fake",
                workspace=tmp_path,
                policy=AgentExecutionPolicy(require_enforcement=False),
            ),
            AgentTurnRequest(message="resume"),
        )
        opened.run.agents.bind_session_transport(transport)
        task = opened.start()
        await opened.waiting(task)
        await opened.complete()
        await task
        final = await opened.run.state.load(DynamicState)
        assert final is not None
        assert len(final.search.rounds) == 2
        healthy = next(item for item in final.workstreams if item.hypothesis_id == "healthy")
        assert healthy.last_error is None
        assert healthy.implementation is not None
        failed = next(item for item in final.search.rounds if item.hypothesis_id == "held")
        assert failed.hypothesis_outcome == "implementation_failed"
        held = next(item for item in final.workstreams if item.hypothesis_id == "held")
        assert "unexpected adapter failure" in (held.last_error or "")
        assert len(opened.calls) == 1
        await opened.start()
        assert len(opened.calls) == 1
        opened.client.close()

    asyncio.run(scenario())


def test_failed_resume_generation_remains_fenced_after_explicit_continuation(
    tmp_path: Path,
) -> None:
    def lose_acknowledgement(_request: AgentTurnRequest) -> None:
        detail = "provider acknowledgement lost after acceptance"
        raise OSError(detail)

    async def scenario() -> None:
        opened = await _open(tmp_path, lose_acknowledgement, submission="continue_failed")
        task = opened.start()
        await opened.waiting(task)
        await opened.complete()
        await task
        final = await opened.run.state.load(DynamicState)
        assert final is not None
        assert len(final.search.rounds) == 2
        assert final.search.rounds[0].hypothesis_outcome == "implementation_failed"
        assert final.search.rounds[1].hypothesis_id == "held"
        assert final.search.rounds[1].hypothesis_outcome != "implementation_failed"
        assert final.workstreams[0].sequence == 2
        assert any(
            intent.stage is IntentStage.BLOCKED and intent.generation == 1
            for intent in final.lifecycle.intents.values()
        )
        await opened.start()
        assert len(opened.calls) == 2
        opened.client.close()

    asyncio.run(scenario())
