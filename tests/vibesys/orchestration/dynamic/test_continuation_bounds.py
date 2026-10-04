"""One charged attempt remains bounded across same-session evaluation yields."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal, cast

import pytest
from tests.vibesys.orchestration.dynamic._support import Script, baseline_run
from tests.vibesys.orchestration.dynamic.test_suspension_shell import (
    IMPLEMENTER,
    AgentClient,
    AgentExecutionPolicy,
    AgentSessionKey,
    AgentSessionSpec,
    AgentTurnRequest,
    ContentDigest,
    DynamicState,
    DynamicWorkstream,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvaluationStepResult,
    EvidenceFingerprints,
    EvidenceKind,
    FakeAgentSessions,
    FakeDriver,
    FakeEvaluationSettlements,
    IntentKind,
    IntentStage,
    LifecycleIntent,
    LifecycleState,
    SemanticEvaluationStage,
    SessionScope,
    StageState,
    WaitingForEvaluation,
    WorkstreamBudget,
    WorkstreamPhase,
    WorkstreamPlan,
)

from vibesys.orchestration.dynamic.models import ImplementerResult
from vibesys.run.dynamic_suspension import (
    EvaluationSuspension,
    EvaluationSuspensionUnresolvedError,
)
from vibesys.run.evaluation_backend import agent_evaluation
from vs_evaluation.api import EvidenceOutcome, FailureKind, PartialMeasurement, TrustedEvidence
from vs_runtime.api import RuntimeContractError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from vibesys.run.dynamic_suspension import EvaluationAttemptBoundError
    from vs_evaluation.api import RepeatedFailure
    from vs_runtime.api import AgentConversation, CandidateWorkspace
    from vs_runtime.api.testing import FakeEvaluation, FakeRun

_TRACEBACK = 'Traceback (most recent call last):\n  File "candidate.py", line 9, in run\n    raise ValueError("broken")\nValueError: broken'


@pytest.mark.parametrize("kind", [FailureKind.TRACEBACK, FailureKind.MEASUREMENT])
@pytest.mark.parametrize("limit", [2, 3, 5])
@pytest.mark.parametrize("yield_case", ["new", "reuse", "mixed"])
@pytest.mark.asyncio
async def test_attempt_bound_stops_before_another_resume_or_submission(
    tmp_path: Path, kind: FailureKind, limit: int, yield_case: Literal["new", "reuse", "mixed"]
) -> None:
    run = baseline_run(tmp_path, Script({}))
    revision = await run.workspaces.root.snapshot("root")
    workspace = await run.workspaces.create_candidate(revision, member_id="held")
    handles = await _submit_failures(run.evaluation, workspace, revision, kind, 0)
    loop = asyncio.get_running_loop()
    calls: list[AgentTurnRequest] = []
    answer: dict[str, object] = {"kind": "waiting_for_evaluation", "handles": [handles[0]]}

    def on_turn(request: AgentTurnRequest) -> None:
        calls.append(request)
        if request.invocation_id is not None:
            if yield_case == "reuse":
                answer["handles"] = [handles[0]]
                return
            if len(calls) > limit + 1:
                answer.clear()
                answer.update(summary="Finished", outcome="continue", next_step="Done")
                return
            submitted = asyncio.run_coroutine_threadsafe(
                _submit_failures(run.evaluation, workspace, revision, kind, len(handles)), loop
            ).result()
            handles.extend(submitted)
            answer["handles"] = (
                [handles[0], handles[-1]] if yield_case == "mixed" else [handles[-1]]
            )
            run.evaluation.advance_time(10)

    session, client = await _session(run, tmp_path, answer, on_turn)
    operation = "held/implementer/1"
    state = DynamicState(
        workstreams=[
            DynamicWorkstream(
                hypothesis_id="held",
                sequence=1,
                planning_call=1,
                parent_revision=revision,
                phase=WorkstreamPhase.IMPLEMENTING,
                budget=WorkstreamBudget(spent=1),
                plan=WorkstreamPlan.model_validate(
                    {
                        "hypothesis_id": "held",
                        "title": "Bounded",
                        "hypothesis": "Improve",
                        "task": "Evaluate",
                        "pass_criteria": "Trusted stage completes",
                    }
                ),
            )
        ],
        lifecycle=LifecycleState(
            intents={
                operation: LifecycleIntent(
                    operation_id=operation,
                    scope_id="held",
                    generation=1,
                    kind=IntentKind.TURN,
                    invocation_id=operation,
                    stage=IntentStage.DISPATCHED,
                )
            }
        ),
    )

    async def commit(label: str) -> None:
        await run.state.commit(state, label=label)

    shell = _prepared_shell(run, state, commit, operation, workspace)
    if limit != 3:
        shell.max_repeated_failures = limit
    await shell.yield_turn(
        0,
        workspace,
        session,
        WaitingForEvaluation(kind="waiting_for_evaluation", handles=(handles[0],)),
    )
    try:
        with pytest.raises(RuntimeContractError) as raised:
            await shell.run_wait(0, workspace, session)
        assert type(raised.value).__name__ == "EvaluationAttemptBoundError"
        error = cast("EvaluationAttemptBoundError", raised.value)
        expected_turns, expected_evaluations = _assert_repeat(
            error.repeated, kind, limit, yield_case
        )
        assert len(calls) == expected_turns
        assert len(state.lifecycle.continuations) == expected_evaluations
        assert (
            sum(
                bool(commit.label and commit.label.endswith("EvaluationSettled"))
                for commit in run.state.commits
            )
            == expected_evaluations
        )
        assert len(handles) == expected_evaluations
        assert state.workstreams[0].phase is WorkstreamPhase.FAILED
        assert state.workstreams[0].budget.spent == 1
        assert state.workstreams[0].last_error
        if limit > 2 and yield_case != "reuse":
            assert error.repeated is not None
            assert error.repeated.instruction in calls[-1].message
        await _assert_restart_has_no_resume(run, workspace, session, calls, expected_turns)
    finally:
        await session.close()
        await workspace.discard()
        client.close()


async def _submit_failures(
    evaluation: FakeEvaluation,
    workspace: CandidateWorkspace,
    revision: str,
    kind: FailureKind,
    limit: int,
) -> list[str]:
    digest = ContentDigest.sha256(b"capture")
    fingerprints = EvidenceFingerprints(
        candidate=digest, evaluator=digest, workload=digest, environment=digest
    )
    settlements = evaluation.settlement_observations or FakeEvaluationSettlements()
    assert isinstance(settlements, FakeEvaluationSettlements)
    handles = []
    for ordinal in (limit,):
        handle = await settlements.submit(
            EvaluationRequest(
                key=f"failure-{ordinal}",
                owner_scope=workspace.id,
                owner_generation=0,
                stages=tuple(
                    EvaluationStep(
                        name=stage.value,
                        payload=SemanticEvaluationStage(
                            snapshot=revision,
                            kind=stage,
                            fingerprints=fingerprints,
                        ).model_dump(mode="json"),
                    )
                    for stage in (EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK)
                ),
            ),
            fingerprints,
        )
        handles.append(handle)
        results = []
        for stage in (EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK):
            evidence = TrustedEvidence(
                evidence_id=ContentDigest.sha256(f"{handle}/{stage.value}".encode()).value,
                evaluation_id=handle,
                stage_name=stage.value,
                kind=stage,
                outcome=EvidenceOutcome.PASSED
                if stage is EvidenceKind.ACCURACY
                else EvidenceOutcome.FAILED,
                fingerprints=fingerprints,
                trusted_inputs=digest,
                accepted_round=0,
                partial_measurement=PartialMeasurement(
                    name="warmup_tokens_per_s", value=70 + ordinal, unit="tok/s", direction="max"
                )
                if kind is FailureKind.MEASUREMENT and stage is EvidenceKind.BENCHMARK
                else None,
            )
            results.append(
                EvaluationStepResult(
                    name=stage.value,
                    state=StageState.SUCCEEDED
                    if stage is EvidenceKind.ACCURACY
                    else StageState.FAILED,
                    failure="stage failed" if stage is EvidenceKind.BENCHMARK else None,
                    result=evidence.model_dump(mode="json")
                    if kind is FailureKind.MEASUREMENT or stage is EvidenceKind.ACCURACY
                    else None,
                )
            )
        failure = _TRACEBACK if kind is FailureKind.TRACEBACK else "warmup timed out"
        settlements.executor.set_state(
            handle, EvaluationState.FAILED, stage_results=tuple(results), failure=failure
        )
        await settlements.coordinator.status(handle)
        report = await settlements.coordinator.recorded_snapshot(handle)
        evaluation.submitted_reports[handle] = report.model_dump_json()
        evaluation.record_agent_evaluation(workspace, agent_evaluation(report))
        evaluation.submitted_revisions[handle] = revision
        evaluation.submitted_generations[handle] = 0
        evaluation.submitted_deadlines[handle] = 1000.0
        evaluation.accepted_evidence[handle] = (
            ContentDigest.sha256(f"{handle}/accuracy".encode()).value,
        )
    evaluation.settlement_observations = settlements
    return handles


async def _assert_restart_has_no_resume(
    run: FakeRun,
    workspace: CandidateWorkspace,
    session: AgentConversation,
    calls: list[AgentTurnRequest],
    limit: int,
) -> None:
    persisted = await run.state.load(DynamicState)
    assert persisted is not None

    async def commit(_label: str) -> None:
        await run.state.commit(persisted)

    restarted = EvaluationSuspension(run, persisted, asyncio.Lock(), commit)
    with pytest.raises(EvaluationSuspensionUnresolvedError, match="no dispatch authority"):
        await restarted.run_wait(0, workspace, session)
    assert len(calls) == limit


async def _session(
    run: FakeRun, root: Path, answer: dict[str, object], on_turn: Callable[[AgentTurnRequest], None]
) -> tuple[AgentConversation, AgentClient]:
    client = AgentClient(FakeDriver(answer=answer, on_turn=on_turn))
    key = AgentSessionKey(SessionScope.MEMBER, f"{IMPLEMENTER.id}:held")
    spec = AgentSessionSpec(
        role=IMPLEMENTER.id,
        provider="fake",
        workspace=root,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    client.run(session_spec=spec, turn=AgentTurnRequest(message="initial"), session_key=key)
    transport = FakeAgentSessions(client)
    transport.bind(key, spec, AgentTurnRequest(message="resume"))
    run.agents.bind_session_transport(transport)
    session = await run.agents.create_session(
        IMPLEMENTER, workspace=run.workspaces.root, member_id="held"
    )
    return session, client


def _assert_repeat(
    repeated: RepeatedFailure | None,
    kind: FailureKind,
    limit: int,
    yield_case: Literal["new", "reuse", "mixed"],
) -> tuple[int, int]:
    if yield_case == "reuse":
        assert repeated is None
        return 2, 1
    assert repeated is not None
    assert repeated.kind is kind
    assert repeated.count == limit
    return limit, limit


def _prepared_shell(
    run: FakeRun,
    state: DynamicState,
    commit: Callable[[str], Awaitable[None]],
    invocation_id: str,
    workspace: CandidateWorkspace,
) -> EvaluationSuspension:
    shell = EvaluationSuspension(run, state, asyncio.Lock(), commit)
    assert workspace.id is not None
    shell.cursors.record(
        invocation_id=invocation_id, workspace_id=workspace.id, preceding_handles=()
    )
    return shell


@pytest.mark.parametrize("cursor_case", ["missing", "changed_prefix"])
@pytest.mark.asyncio
async def test_unavailable_attempt_history_ends_before_resume(
    tmp_path: Path, cursor_case: str
) -> None:
    run = baseline_run(tmp_path, Script({}))
    revision = await run.workspaces.root.snapshot("root")
    workspace = await run.workspaces.create_candidate(revision, member_id="held")
    handles = await _submit_failures(run.evaluation, workspace, revision, FailureKind.TRACEBACK, 0)
    calls: list[AgentTurnRequest] = []
    session, client = await _session(
        run, tmp_path, {"kind": "waiting_for_evaluation", "handles": handles}, calls.append
    )
    operation = "held/implementer/1"
    state = _retry_state(operation, revision)

    async def commit(label: str) -> None:
        await run.state.commit(state, label=label)

    shell = EvaluationSuspension(run, state, asyncio.Lock(), commit)
    if cursor_case == "changed_prefix":
        assert workspace.id is not None
        shell.cursors.record(
            invocation_id=operation,
            workspace_id=workspace.id,
            preceding_handles=("previous-submission",),
        )
    try:
        await shell.yield_turn(
            0,
            workspace,
            session,
            WaitingForEvaluation(kind="waiting_for_evaluation", handles=tuple(handles)),
        )
        with pytest.raises(RuntimeContractError) as raised:
            await shell.run_wait(0, workspace, session)
        assert type(raised.value).__name__ == "EvaluationAttemptHistoryUnavailableError"
        assert len(calls) == 1
        assert state.workstreams[0].phase is WorkstreamPhase.FAILED
        assert state.workstreams[0].last_error
        await _assert_restart_has_no_resume(run, workspace, session, calls, 1)
    finally:
        await session.close()
        await workspace.discard()
        client.close()


@pytest.mark.asyncio
async def test_fresh_retry_cursor_excludes_previous_attempt_failures(tmp_path: Path) -> None:
    run = baseline_run(tmp_path, Script({}))
    revision = await run.workspaces.root.snapshot("root")
    workspace = await run.workspaces.create_candidate(revision, member_id="held")
    prior = []
    for ordinal in range(3):
        prior.extend(
            await _submit_failures(
                run.evaluation, workspace, revision, FailureKind.TRACEBACK, ordinal
            )
        )
    operation = "held/implementer/2"
    state = _retry_state(operation, revision)

    async def commit(label: str) -> None:
        await run.state.commit(state, label=label)

    shell = EvaluationSuspension(run, state, asyncio.Lock(), commit)
    assert workspace.id is not None
    shell.cursors.record(
        invocation_id=operation, workspace_id=workspace.id, preceding_handles=tuple(prior)
    )
    shell.max_repeated_failures = 2
    fresh = await _submit_failures(run.evaluation, workspace, revision, FailureKind.TRACEBACK, 3)
    calls: list[AgentTurnRequest] = []
    answer: dict[str, object] = {"kind": "waiting_for_evaluation", "handles": fresh}
    session, client = await _session(run, tmp_path, answer, calls.append)
    answer.clear()
    answer.update(summary="Finished", outcome="continue", next_step="Done")
    try:
        await shell.yield_turn(
            0,
            workspace,
            session,
            WaitingForEvaluation(kind="waiting_for_evaluation", handles=tuple(fresh)),
        )
        result, _ = await shell.run_wait(0, workspace, session)
        assert isinstance(result, ImplementerResult)
        assert result.summary == "Finished"
        assert len(calls) == 2
        assert state.workstreams[0].phase is WorkstreamPhase.IMPLEMENTING
        assert state.workstreams[0].budget.spent == 2
        cursor = shell.cursors.read(operation)
        assert cursor is not None
        assert cursor.preceding_handles == tuple(prior)
    finally:
        await session.close()
        await workspace.discard()
        client.close()


def _retry_state(operation: str, revision: str) -> DynamicState:
    return DynamicState(
        workstreams=[
            DynamicWorkstream(
                hypothesis_id="held",
                sequence=1,
                planning_call=1,
                parent_revision=revision,
                phase=WorkstreamPhase.IMPLEMENTING,
                budget=WorkstreamBudget(spent=2),
                plan=WorkstreamPlan.model_validate(
                    {
                        "hypothesis_id": "held",
                        "title": "Bounded",
                        "hypothesis": "Improve",
                        "task": "Evaluate",
                        "pass_criteria": "Trusted stage completes",
                    }
                ),
            )
        ],
        lifecycle=LifecycleState(
            intents={
                operation: LifecycleIntent(
                    operation_id=operation,
                    scope_id="held",
                    generation=1,
                    kind=IntentKind.TURN,
                    invocation_id=operation,
                    stage=IntentStage.DISPATCHED,
                )
            }
        ),
    )
