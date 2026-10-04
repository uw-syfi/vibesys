"""Public suspension, shared evaluation and bounded continuation regressions."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal, cast

import pytest
from tests.vibesys.orchestration.dynamic._profile_release_support import profile_release_effects
from tests.vibesys.orchestration.dynamic._support import (
    Script,
    baseline_run,
    dynamic_options,
    implementation,
)
from tests.vibesys.orchestration.dynamic.test_plugin_suspension import ResumeScript, _open
from tests.vibesys.orchestration.dynamic.test_suspension_deadline import (
    _drive,
    _has_resume_requests,
    _park_shared_wait,
)

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.orchestration.dynamic.agents import IMPLEMENTER
from vibesys.orchestration.dynamic.lifecycle import (
    BlockIntent,
    CompleteIntent,
    IntentKind,
    IntentStage,
    LifecycleIntent,
    LifecycleState,
)
from vibesys.orchestration.dynamic.models import (
    DynamicState,
    DynamicWorkstream,
    ImplementerResult,
    WaitingForEvaluation,
    WorkstreamBudget,
    WorkstreamPhase,
    WorkstreamPlan,
)
from vibesys.run.dynamic_suspension import (
    EvaluationSuspension,
    EvaluationSuspensionInvariantError,
    EvaluationSuspensionUnresolvedError,
)
from vibesys.run.evaluation_backend import SemanticEvaluationStage, agent_evaluation
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
    ContentDigest,
    EvaluationAgentRole,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvaluationStepResult,
    EvidenceFingerprints,
    EvidenceKind,
    EvidenceOutcome,
    FailureKind,
    OwnedEvaluationDependencies,
    PartialMeasurement,
    StageState,
    SubmitCall,
    SubmittedReply,
    TrustedEvidence,
)
from vs_evaluation.api.testing import FakeEvaluationSettlements
from vs_runtime.api import AgentToolBindingContext, RuntimeContractError
from vs_runtime.api.infrastructure import TrustedEvaluationPlan

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from tests.vibesys.orchestration.dynamic._profile_release_support import ProfileReleaseEffects
    from tests.vibesys.orchestration.dynamic.test_plugin_suspension import _Scenario

    from vibesys.run.dynamic_suspension import EvaluationAttemptBoundError
    from vs_evaluation.api import RepeatedFailure
    from vs_runtime.api import AgentConversation, AgentSession, CandidateWorkspace
    from vs_runtime.api.testing import FakeEvaluation, FakeRun


@pytest.mark.parametrize("elapsed_s", [240, 301, 420])
def test_host_wait_spends_no_agent_calls_or_attempts(elapsed_s: int, tmp_path: Path) -> None:
    """A multi-minute wait crosses the coordinator bound without agent polling."""

    async def scenario() -> None:
        run = baseline_run(tmp_path, Script({}))
        root = await run.workspaces.root.snapshot("root")
        workspace = await run.workspaces.create_candidate(root, member_id="held")
        assert workspace.id is not None
        calls: list[AgentTurnRequest] = []
        client = AgentClient(
            FakeDriver(
                answer={
                    "summary": "Trusted result checked.",
                    "outcome": "continue",
                    "next_step": "Done.",
                },
                on_turn=calls.append,
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
        session = await run.agents.create_session(
            IMPLEMENTER, workspace=workspace, member_id="held"
        )
        settlements = FakeEvaluationSettlements()
        digest = ContentDigest.sha256(b"immutable capture")
        handle = await settlements.submit(
            EvaluationRequest(
                key="held",
                owner_scope=workspace.id,
                owner_generation=0,
                stages=(
                    EvaluationStep(
                        name="benchmark",
                        payload=SemanticEvaluationStage(
                            snapshot=root,
                            kind=EvidenceKind.BENCHMARK,
                            fingerprints=EvidenceFingerprints(
                                candidate=digest,
                                evaluator=digest,
                                workload=digest,
                                environment=digest,
                            ),
                        ).model_dump(mode="json"),
                    ),
                ),
            ),
            EvidenceFingerprints(
                candidate=digest, evaluator=digest, workload=digest, environment=digest
            ),
        )
        run.evaluation.settlement_observations = settlements
        run.evaluation.submitted_revisions[handle] = root
        run.evaluation.submitted_generations[(workspace.id, handle)] = 0
        run.evaluation.submitted_deadlines[handle] = 1000.0
        run.evaluation.accepted_evidence[handle] = ("a" * 64,)
        invocation = "held/implementer/1"
        plan = WorkstreamPlan.model_validate(
            {
                "hypothesis_id": "held",
                "title": "Held evaluation",
                "hypothesis": "Held candidate improves.",
                "task": "Evaluate the candidate.",
                "pass_criteria": "Trusted measurement completes.",
            }
        )
        state = DynamicState(
            workstreams=[
                DynamicWorkstream(
                    hypothesis_id="held",
                    sequence=1,
                    planning_call=1,
                    plan=plan,
                    parent_revision=root,
                    phase=WorkstreamPhase.IMPLEMENTING,
                    budget=WorkstreamBudget(spent=1),
                )
            ],
            lifecycle=LifecycleState(
                intents={
                    invocation: LifecycleIntent(
                        operation_id=invocation,
                        scope_id="held",
                        generation=1,
                        kind=IntentKind.TURN,
                        invocation_id=invocation,
                        stage=IntentStage.DISPATCHED,
                    )
                }
            ),
        )

        async def commit(_label: str) -> None:
            await run.state.commit(state)

        shell = _shell_with_cursor(run, state, commit, invocation, workspace.id)
        await shell.yield_turn(
            0,
            workspace,
            session,
            WaitingForEvaluation(kind="waiting_for_evaluation", handles=(handle,)),
        )
        budget = state.workstreams[0].budget
        initial_calls = len(calls)
        settlements.executor.wait_started.clear()
        task = asyncio.create_task(shell.run_wait(0, workspace, session))
        await settlements.executor.wait_started.wait()
        settlements.executor.clock.advance(elapsed_s)
        assert len(calls) == initial_calls
        assert state.workstreams[0].budget == budget
        settlements.executor.set_state(
            handle,
            EvaluationState.SUCCEEDED,
            stage_results=(EvaluationStepResult(name="benchmark", state=StageState.SUCCEEDED),),
        )
        await settlements.coordinator.status(handle)
        await _record_report(run, settlements, handle, workspace)
        reply, _ = await task
        assert isinstance(reply, ImplementerResult)
        assert len(calls) == initial_calls + 1
        assert calls[-1].expected_provider_session_id == session.checkpoint().provider_session_id
        assert state.workstreams[0].budget == budget
        await session.close()
        await workspace.discard()
        client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("event_type", [BlockIntent, CompleteIntent])
def test_unknown_lifecycle_operation_remains_an_invariant_failure(
    tmp_path: Path, event_type: type[BlockIntent] | type[CompleteIntent]
) -> None:
    async def scenario() -> None:
        run = baseline_run(tmp_path, Script({}))
        state = DynamicState()

        async def commit(_label: str) -> None:
            await run.state.commit(state)

        shell = EvaluationSuspension(run, state, asyncio.Lock(), commit)
        before = state.model_dump_json()
        with pytest.raises(EvaluationSuspensionInvariantError) as raised:
            await shell.apply(event_type(operation_id="unknown-operation"))
        assert isinstance(raised.value.__cause__, (KeyError, ValueError))
        assert state.model_dump_json() == before
        assert run.state.commits == ()

    asyncio.run(scenario())


def _shell_with_cursor(
    run: FakeRun,
    state: DynamicState,
    commit: Callable[[str], Awaitable[None]],
    invocation: str,
    workspace_id: str | None,
) -> EvaluationSuspension:
    assert workspace_id is not None
    shell = EvaluationSuspension(run, state, asyncio.Lock(), commit)
    shell.cursors.record(invocation_id=invocation, workspace_id=workspace_id, preceding_handles=())
    return shell


async def _record_report(
    run: FakeRun, settlements: FakeEvaluationSettlements, handle: str, workspace: CandidateWorkspace
) -> None:
    report = await settlements.coordinator.recorded_snapshot(handle)
    run.evaluation.submitted_reports[handle] = report.model_dump_json()
    run.evaluation.record_agent_evaluation(workspace, agent_evaluation(report))


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
        assert workspace.id is not None
        evaluation.submitted_generations[(workspace.id, handle)] = 0
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


@pytest.mark.asyncio
@pytest.mark.parametrize("reopened", [False, True], ids=["foreign-scope", "reopened-requester"])
@pytest.mark.parametrize("mode", ["settle", "cancel", "stale"])
async def test_workstream_b_joins_a_capture_and_resumes_once(
    tmp_path: Path, *, reopened: bool, mode: Literal["settle", "cancel", "stale"]
) -> None:
    """B's unchanged candidate shares A's physical work without owning A's wait."""
    base = baseline_run(tmp_path, Script({}))
    base.workspaces.set_default_patch("identical candidate content")
    root = await base.workspaces.root.snapshot("root")
    owner = await base.workspaces.create_candidate(root, member_id="a")
    requester = await base.workspaces.create_candidate(root, member_id="b")
    assert owner.id is not None
    assert requester.id is not None
    effects = profile_release_effects(
        tmp_path,
        base,
        plan=TrustedEvaluationPlan(
            accuracy_timeout_seconds=60,
            framework_setup_timeout_seconds=30,
        ),
    )
    calls: list[AgentTurnRequest] = []
    client = AgentClient(
        FakeDriver(
            answer={
                "summary": "Shared result checked.",
                "outcome": "continue",
                "next_step": "Done.",
            },
            on_turn=calls.append,
        )
    )
    try:
        handle = await _shared_join(effects, owner, requester, reopened=reopened)
        session = await _shared_session(base, client, tmp_path, requester)
        invocation = "b/implementer/1"
        state = DynamicState(
            workstreams=[
                DynamicWorkstream(
                    hypothesis_id="b",
                    sequence=1,
                    planning_call=1,
                    plan=WorkstreamPlan.model_validate(
                        {
                            "hypothesis_id": "b",
                            "title": "Shared capture",
                            "hypothesis": "Unchanged candidate",
                            "task": "Check shared result",
                            "pass_criteria": "Trusted capture settles",
                        }
                    ),
                    parent_revision=root,
                    phase=WorkstreamPhase.IMPLEMENTING,
                    budget=WorkstreamBudget(spent=1),
                )
            ],
            lifecycle=LifecycleState(
                intents={
                    invocation: LifecycleIntent(
                        operation_id=invocation,
                        scope_id="b",
                        generation=1,
                        kind=IntentKind.TURN,
                        invocation_id=invocation,
                        stage=IntentStage.DISPATCHED,
                    )
                }
            ),
        )

        async def commit(label: str) -> None:
            await effects.run.state.commit(state, label=label)

        shell = EvaluationSuspension(effects.run, state, asyncio.Lock(), commit)
        shell.cursors.record(
            invocation_id=invocation, workspace_id=requester.id, preceding_handles=()
        )
        await shell.yield_turn(
            0,
            requester,
            session,
            WaitingForEvaluation(
                kind="waiting_for_evaluation",
                handles=(handle,),
            ),
        )
        continuation = next(iter(state.lifecycle.continuations.values()))
        assert continuation.evaluation_scope_id == requester.id
        assert continuation.evaluation_generation == int(reopened)
        initial_calls = len(calls)
        budget = state.workstreams[0].budget
        await _drive(shell, effects, continuation, mode)
        if mode == "stale":
            await _shared_join(effects, owner, requester, reopened=True)
            with pytest.raises(EvaluationSuspensionInvariantError, match="requester generation"):
                await shell.run_wait(0, requester, session)
            assert len(calls) == initial_calls
            assert state.workstreams[0].budget == budget
            await session.close()
            return
        reply, acknowledgement = await shell.run_wait(0, requester, session)
        assert isinstance(reply, ImplementerResult)
        assert len(calls) == initial_calls + 1
        assert calls[-1].expected_provider_session_id == session.checkpoint().provider_session_id
        assert state.workstreams[0].budget == budget
        await _assert_capture(effects, owner, state, calls[-1], cancel=mode == "cancel")
        await shell.apply(CompleteIntent(operation_id=acknowledgement))
        assert not await _has_resume_requests(shell)
        assert len(calls) == initial_calls + 1
        await session.close()
    finally:
        client.close()
        await effects.close()


async def _shared_join(
    effects: ProfileReleaseEffects,
    owner: CandidateWorkspace,
    requester: CandidateWorkspace,
    *,
    reopened: bool,
) -> str:
    assert owner.id is not None
    assert requester.id is not None
    for workspace in (owner, requester):
        effects.backend.bind(AgentToolBindingContext(IMPLEMENTER, workspace, workspace.id, str))
    if reopened:
        await effects.service.cancel_scope(requester.id)
        await effects.service.reopen_scope(requester.id)
    handles = []
    for workspace in (owner, requester):
        assert workspace.id is not None
        grant = effects.service.grant(
            principal_id=workspace.id,
            role=EvaluationAgentRole.IMPLEMENTER,
            scope_id=workspace.id,
        )
        submitted = await effects.service.dispatch(
            SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.ACCURACY,))
        )
        assert isinstance(submitted, SubmittedReply)
        handles.append(submitted.handle_id)
    assert handles[0] == handles[1]
    assert len(effects.executor.submissions) == 1
    return handles[1]


async def _shared_session(
    base: FakeRun,
    client: AgentClient,
    tmp_path: Path,
    requester: CandidateWorkspace,
) -> AgentSession:
    key = AgentSessionKey.for_member(IMPLEMENTER.id, "b")
    spec = AgentSessionSpec(
        role=IMPLEMENTER.id,
        provider="fake",
        workspace=tmp_path,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    client.run(session_spec=spec, turn=AgentTurnRequest(message="initial"), session_key=key)
    transport = FakeAgentSessions(client)
    transport.bind(key, spec, AgentTurnRequest(message="resume"))
    base.agents.bind_session_transport(transport)
    return await base.agents.create_session(IMPLEMENTER, workspace=requester, member_id="b")


async def _assert_capture(
    effects: ProfileReleaseEffects,
    owner: CandidateWorkspace,
    state: DynamicState,
    last_call: AgentTurnRequest,
    *,
    cancel: bool,
) -> None:
    continuation = next(iter(state.lifecycle.continuations.values()))
    handle = continuation.dependencies[0].handle
    if cancel:
        assert await effects.backend.status(handle) is EvaluationState.QUEUED
        assert effects.executor.backend.active_count == 1
    else:
        report = await effects.backend.recorded_snapshot(handle)
        assert report.request.owner_scope == owner.id
        assert report.request.owner_generation == 0
        assert len(continuation.settlements) == 1
        assert "Terminal status: passed" in last_call.message


@pytest.mark.parametrize("kind", [FailureKind.TRACEBACK, FailureKind.MEASUREMENT])
@pytest.mark.parametrize("omitted", [False, True])
@pytest.mark.parametrize("return_final", [False, True])
@pytest.mark.asyncio
async def test_public_plugin_bounds_incremental_evaluations(
    tmp_path: Path, kind: FailureKind, *, omitted: bool, return_final: bool
) -> None:
    answer: dict[str, object] = {}
    loop = asyncio.get_running_loop()
    submissions = 1

    def resumed(request: AgentTurnRequest) -> None:
        nonlocal submissions
        if request.invocation_id is None:
            return
        if submissions >= 5:
            answer.clear()
            answer.update(implementation("held"))
            return
        workspace = opened.run.workspaces.candidates[-1]
        handle = asyncio.run_coroutine_threadsafe(
            _submit_failures(
                opened.evaluation,
                workspace,
                opened.evaluation.submitted_revisions[opened.handle],
                kind,
                submissions,
            ),
            loop,
        ).result()[0]
        submissions += 1
        if omitted and submissions == 2:
            asyncio.run_coroutine_threadsafe(
                _submit_failures(
                    opened.evaluation,
                    workspace,
                    opened.evaluation.submitted_revisions[opened.handle],
                    kind,
                    submissions,
                ),
                loop,
            ).result()
            submissions += 1
        opened.evaluation.advance_time(10)
        if return_final and submissions == 4:
            answer.clear()
            answer.update(implementation("held"))
        else:
            answer.update(kind="waiting_for_evaluation", handles=[handle])

    opened = await _open(tmp_path, on_resume=ResumeScript(answer, resumed))
    try:
        task = asyncio.ensure_future(
            PLUGIN.orchestrate(
                opened.runtime,
                dynamic_options(
                    max_in_flight=1,
                    max_rounds=1,
                    judge_every=100,
                    max_retries_per_round=1,
                    max_repeated_failures=3,
                ),
            )
        )
        await opened.waiting(task)
        await _fail_first(opened, kind)
        try:
            await task
        finally:
            assert submissions == 4  # The seed predates this charged attempt.
        final = await opened.run.state.load(DynamicState)
        assert final is not None
        assert final.workstreams[0].phase is WorkstreamPhase.FAILED
        assert final.workstreams[0].budget.spent == 1
        assert len(final.search.rounds) == 1
        assert len(opened.calls) == (3 if omitted else 4)
        if kind is FailureKind.MEASUREMENT and not omitted:
            assert "not changed the bottleneck" in opened.calls[-1].message
    finally:
        opened.client.close()


async def _fail_first(opened: _Scenario, kind: FailureKind) -> None:
    digest = ContentDigest.sha256(b"immutable capture")
    evidence = TrustedEvidence(
        evidence_id="a" * 64,
        evaluation_id=opened.handle,
        stage_name="benchmark",
        kind=EvidenceKind.BENCHMARK,
        outcome=EvidenceOutcome.FAILED,
        fingerprints=EvidenceFingerprints(
            candidate=digest, evaluator=digest, workload=digest, environment=digest
        ),
        trusted_inputs=digest,
        accepted_round=0,
        partial_measurement=PartialMeasurement(
            name="warmup_tokens_per_s", value=70, direction="max", unit="tok/s"
        )
        if kind is FailureKind.MEASUREMENT
        else None,
    )
    failure = _TRACEBACK if kind is FailureKind.TRACEBACK else "warmup timed out"
    opened.evaluations.executor.set_state(
        opened.handle,
        EvaluationState.FAILED,
        failure=failure,
        stage_results=(
            EvaluationStepResult(
                name="benchmark",
                state=StageState.SUCCEEDED
                if kind is FailureKind.MEASUREMENT
                else StageState.FAILED,
                failure=failure if kind is FailureKind.TRACEBACK else None,
                result=evidence.model_dump(mode="json")
                if kind is FailureKind.MEASUREMENT
                else None,
            ),
        ),
    )
    await opened.evaluations.coordinator.status(opened.handle)
    report = await opened.evaluations.coordinator.recorded_snapshot(opened.handle)
    opened.evaluation.submitted_reports[opened.handle] = report.model_dump_json()
    opened.evaluation.record_agent_evaluation(
        opened.run.workspaces.candidates[-1], agent_evaluation(report)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reopened", [False, True], ids=["generation-zero", "generation-one"])
async def test_parked_workstream_resumes_from_shared_capture_after_owner_settles(
    tmp_path: Path, *, reopened: bool
) -> None:
    base = baseline_run(tmp_path, Script({}))
    base.workspaces.set_default_patch("identical shared captured candidate")
    root = await base.workspaces.root.snapshot("root")
    owner = await base.workspaces.create_candidate(root, member_id="a")
    requester = await base.workspaces.create_candidate(root, member_id="b")
    assert owner.id is not None
    assert requester.id is not None
    effects = profile_release_effects(
        tmp_path,
        base,
        plan=TrustedEvaluationPlan(accuracy_timeout_seconds=60, framework_setup_timeout_seconds=30),
    )
    calls: list[AgentTurnRequest] = []
    client = AgentClient(
        FakeDriver(
            answer={
                "summary": "Shared result checked.",
                "outcome": "continue",
                "next_step": "Done.",
            },
            on_turn=calls.append,
        )
    )
    try:
        handle = await _shared_join(effects, owner, requester, reopened=reopened)
        session = await _shared_session(base, client, tmp_path, requester)
        state = _shared_park_state(root)

        async def commit(label: str) -> None:
            await effects.run.state.commit(state, label=label)

        shell = EvaluationSuspension(effects.run, state, asyncio.Lock(), commit)
        invocation = "b/implementer/1"
        shell.cursors.record(
            invocation_id=invocation, workspace_id=requester.id, preceding_handles=()
        )
        await shell.yield_turn(
            0,
            requester,
            session,
            WaitingForEvaluation(kind="waiting_for_evaluation", handles=(handle,)),
        )
        budget = state.workstreams[0].budget
        initial_calls = len(calls)
        await _park_shared_wait(shell, effects)
        assert state.workstreams[0].phase is WorkstreamPhase.PARKED
        assert state.workstreams[0].budget == budget
        assert await effects.backend.status(handle) is EvaluationState.QUEUED
        assert effects.executor.backend.active_count == 1
        await effects.run.evaluation.settlements().observe(
            OwnedEvaluationDependencies(scope_id=owner.id, generation=0, handles=(handle,))
        )
        await _assert_pending_park(shell, state, calls, initial_calls)
        effects.executor.set_state(
            handle,
            EvaluationState.SUCCEEDED,
            stage_results=(EvaluationStepResult(name="accuracy", state=StageState.SUCCEEDED),),
        )
        await effects.backend.status(handle)
        continuation_id = next(iter(state.lifecycle.continuations))
        await shell.reopen_evaluation_wait(continuation_id, ())
        reply, acknowledgement = await shell.run_wait(0, requester, session)
        assert isinstance(reply, ImplementerResult)
        assert state.workstreams[0].budget == budget
        assert len(calls) == initial_calls + 1
        assert "Terminal status: passed" in calls[-1].message
        assert len(effects.executor.submissions) == 1
        await shell.apply(CompleteIntent(operation_id=acknowledgement))
        assert not await _has_resume_requests(shell)
        await session.close()
    finally:
        client.close()
        await effects.close()


def _shared_park_state(root: str) -> DynamicState:
    invocation = "b/implementer/1"
    return DynamicState(
        workstreams=[
            DynamicWorkstream(
                hypothesis_id="b",
                sequence=1,
                planning_call=1,
                plan=WorkstreamPlan.model_validate(
                    {
                        "hypothesis_id": "b",
                        "title": "Shared capture",
                        "hypothesis": "Unchanged candidate",
                        "task": "Check shared result",
                        "pass_criteria": "Trusted capture settles",
                    }
                ),
                parent_revision=root,
                phase=WorkstreamPhase.IMPLEMENTING,
                budget=WorkstreamBudget(spent=1),
            )
        ],
        lifecycle=LifecycleState(
            intents={
                invocation: LifecycleIntent(
                    operation_id=invocation,
                    scope_id="b",
                    generation=1,
                    kind=IntentKind.TURN,
                    invocation_id=invocation,
                    stage=IntentStage.DISPATCHED,
                )
            }
        ),
    )


async def _assert_pending_park(
    shell: EvaluationSuspension,
    state: DynamicState,
    calls: list[AgentTurnRequest],
    initial_calls: int,
) -> None:
    continuation_id = next(iter(state.lifecycle.continuations))
    with pytest.raises(EvaluationSuspensionUnresolvedError, match="unresolved"):
        await shell.reopen_evaluation_wait(continuation_id, ())
    assert state.workstreams[0].phase is WorkstreamPhase.PARKED
    assert len(calls) == initial_calls
