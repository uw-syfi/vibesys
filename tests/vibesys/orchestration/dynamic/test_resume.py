"""Resuming a stopped or crashed run from durable state."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.vibesys.orchestration.dynamic._support import (
    INPUT_BASELINE,
    Script,
    baseline_run,
    dynamic_options,
    implementation,
    portfolio,
    throughput,
)

from vibesys.orchestration.dynamic import (
    PLUGIN,
    DynamicState,
    PortfolioPlan,
)
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vibesys.orchestration.dynamic.models import DurableStateCommitError
from vs_runtime.api import (
    AgentCapability,
    BenchmarkEvaluation,
    CandidateProfile,
    CandidateProfileStatus,
    MetricDirection,
    RunFacts,
    RunStatus,
    RuntimeContractError,
)
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

    from vs_runtime.api import AgentRole


@pytest.mark.parametrize(
    ("durable_phase", "commit_label", "judge_replies", "benchmark_results"),
    [
        pytest.param("implemented", "dynamic: recover reviewed", 2, 1, id="implemented"),
        pytest.param("reviewed", "dynamic: recover evaluated", 1, 2, id="reviewed"),
        pytest.param("evaluated", "dynamic: record hypothesis recover", 1, 1, id="evaluated"),
    ],
)
def test_resume_completes_durable_work_without_repeating_finished_stages(
    tmp_path: Path,
    durable_phase: str,
    commit_label: str,
    judge_replies: int,
    benchmark_results: int,
) -> None:
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("recover")],
            IMPLEMENTER.id: [implementation("recover")],
            JUDGE.id: [
                {"passed": True, "analysis": "Candidate is correct."} for _ in range(judge_replies)
            ],
        }
    )

    async def scenario() -> tuple[FakeRun, RunStatus]:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(
                domain_id="generic",
                objective="Improve throughput.",
                benchmark_configured=True,
            ),
            responder=script.respond,
            supported_extra_tools={"evaluation", "profiler"},
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
                AgentCapability.DURABLE_TURN_CONTINUATION,
            },
            supports_parallel_candidates=True,
        )
        run.evaluation.script_benchmark(
            INPUT_BASELINE,
            *(
                BenchmarkEvaluation(
                    executed=True,
                    metric_name="throughput",
                    metric_value=12.0,
                    metric_direction=MetricDirection.MAXIMIZE,
                    row={"throughput": 12.0},
                )
                for _ in range(benchmark_results)
            ),
        )
        run.state.script_commit_at(commit_label, RuntimeError("stop at durable stage barrier"))
        with pytest.raises(DurableStateCommitError):
            await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1))
        interrupted = await run.state.load(DynamicState)
        assert interrupted is not None
        assert interrupted.workstreams[0].phase.value == durable_phase
        status = await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1))
        return run, status

    run, status = asyncio.run(scenario())

    assert status is RunStatus.SUCCEEDED
    assert len([s for s in run.agents.sessions if s.role.id == ORCHESTRATOR.id]) == 1
    assert len([s for s in run.agents.sessions if s.role.id == IMPLEMENTER.id]) == 1
    assert len([s for s in run.agents.sessions if s.role.id == JUDGE.id]) == judge_replies
    # The input baseline is measured once and reused on resume.
    assert len(run.evaluation.benchmark_calls) == 1 + benchmark_results
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    assert state.workstreams[0].phase.value == "evaluated"
    assert len(state.search.rounds) == 1
    assert state.adoption_pending is False


def test_resumed_rejected_evaluation_drives_a_correction_attempt(tmp_path: Path) -> None:
    feedback = "benchmark mismatch"
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("recover")],
            IMPLEMENTER.id: [implementation("first"), implementation("corrected")],
            JUDGE.id: [
                {"passed": True, "analysis": "First candidate is reviewable."},
                {"passed": True, "analysis": "Correction is reviewable."},
            ],
        }
    )

    async def scenario() -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(
                domain_id="generic",
                objective="Improve throughput.",
                benchmark_configured=True,
            ),
            responder=script.respond,
            supported_extra_tools={"evaluation", "profiler"},
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
                AgentCapability.DURABLE_TURN_CONTINUATION,
            },
            supports_parallel_candidates=True,
        )
        run.evaluation.script_benchmark(
            INPUT_BASELINE,
            BenchmarkEvaluation(executed=True, feedback=feedback),
            BenchmarkEvaluation(
                executed=True,
                metric_name="throughput",
                metric_value=12.0,
                metric_direction=MetricDirection.MAXIMIZE,
                row={"throughput": 12.0},
            ),
        )
        run.state.script_commit_at(
            "dynamic: recover feedback", RuntimeError("stop after rejected evaluation")
        )
        with pytest.raises(DurableStateCommitError):
            await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1, max_retries_per_round=2))
        interrupted = await run.state.load(DynamicState)
        assert interrupted is not None
        assert interrupted.workstreams[0].phase.value == "evaluated"
        assert interrupted.workstreams[0].evaluation is not None
        assert not interrupted.workstreams[0].evaluation.accepted

        await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1, max_retries_per_round=2))
        return run

    run = asyncio.run(scenario())

    implementer_messages = [message for role, _, message in script.calls if role == IMPLEMENTER.id]
    assert len(implementer_messages) == 2
    assert f"Trusted evaluation failed: {feedback}" in implementer_messages[1]
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    assert state.workstreams[0].budget.spent == 2
    assert state.workstreams[0].evaluation is not None
    assert state.workstreams[0].evaluation.accepted


def test_cancelled_dispatched_attempt_blocks_replay_without_replanning(tmp_path: Path) -> None:
    """Ambiguous initial provider acceptance cannot be refunded or blindly replayed."""
    orchestrating: asyncio.Future[RunStatus] | None = None
    implementer_calls = 0

    def respond(
        role: AgentRole,
        _history: tuple[str, ...],
        _message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        nonlocal implementer_calls
        if role.id == ORCHESTRATOR.id:
            return portfolio("interrupted")
        if role.id == IMPLEMENTER.id:
            implementer_calls += 1
            if implementer_calls == 1:
                assert orchestrating is not None
                orchestrating.cancel()
            return implementation("interrupted")
        return {"passed": True, "analysis": "Candidate is correct."}

    async def scenario() -> FakeRun:
        nonlocal orchestrating
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(domain_id="generic", objective="Improve.", benchmark_configured=True),
            responder=respond,
            supported_extra_tools={"evaluation", "profiler"},
            supports_parallel_candidates=True,
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
                AgentCapability.DURABLE_TURN_CONTINUATION,
            },
        )
        run.evaluation.script_benchmark(
            INPUT_BASELINE,
            BenchmarkEvaluation(
                executed=True,
                metric_name="throughput",
                metric_value=10.0,
                metric_direction=MetricDirection.MAXIMIZE,
                row={"throughput": 10.0},
            ),
        )
        options = dynamic_options(max_in_flight=1)
        orchestrating = asyncio.ensure_future(PLUGIN.orchestrate(run, options))
        with pytest.raises(asyncio.CancelledError):
            await orchestrating
        with pytest.raises(RuntimeContractError, match="unresolved"):
            await PLUGIN.orchestrate(run, options)
        return run

    run = asyncio.run(scenario())
    assert implementer_calls == 1
    assert len([s for s in run.agents.sessions if s.role.id == ORCHESTRATOR.id]) == 1
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    assert [item.hypothesis_id for item in state.workstreams] == ["interrupted"]
    assert state.workstreams[0].budget.spent == 1
    assert state.workstreams[0].phase.value == "implementing"
    assert state.workstreams[0].budget.refunded == 0
    assert not state.search.rounds
    assert state.winner_revision is None


def test_repeated_recovery_of_ambiguous_attempt_never_reinvokes_or_refunds(tmp_path: Path) -> None:
    """Repeated recovery of the same unknown turn preserves its original charge."""
    orchestrating: asyncio.Future[RunStatus] | None = None
    implementer_calls = 0

    def respond(
        role: AgentRole,
        _history: tuple[str, ...],
        _message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        nonlocal implementer_calls
        if role.id == ORCHESTRATOR.id:
            return portfolio("crashing")
        implementer_calls += 1
        # Every implementation attempt is interrupted, like a process crash.
        assert orchestrating is not None
        orchestrating.cancel()
        return implementation("crashing")

    async def scenario() -> FakeRun:
        nonlocal orchestrating
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(domain_id="generic", objective="Improve.", benchmark_configured=True),
            responder=respond,
            supported_extra_tools={"evaluation", "profiler"},
            supports_parallel_candidates=True,
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
                AgentCapability.DURABLE_TURN_CONTINUATION,
            },
        )
        options = dynamic_options(max_in_flight=1, max_retries_per_round=1)
        orchestrating = asyncio.ensure_future(PLUGIN.orchestrate(run, options))
        with pytest.raises(asyncio.CancelledError):
            await orchestrating
        for _recovery in range(2):
            with pytest.raises(RuntimeContractError, match="unresolved"):
                await PLUGIN.orchestrate(run, options)
        return run

    run = asyncio.run(scenario())
    assert implementer_calls == 1

    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    assert state.workstreams[0].phase.value == "implementing"
    assert state.workstreams[0].budget.spent == 1
    assert state.workstreams[0].budget.refunded == 0
    assert not state.search.rounds
    assert state.winner_revision is None


class _StopRequestedError(RuntimeError):
    """Synthetic cooperative stop raised at a control checkpoint."""


def test_multi_epoch_run_with_a_rejected_workstream_resumes_after_a_stop(
    tmp_path: Path,
) -> None:
    """Two 2-wide epochs, one review rejection, a stop between epochs, and resume.

    The stop lands at the epoch boundary; resume must not replan the finished
    epoch, must let the planner continue the rejected hypothesis, and must
    adopt the best trusted candidate across both epochs.
    """
    run: FakeRun | None = None
    planner_calls = 0

    def respond(
        role: AgentRole,
        _history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        nonlocal planner_calls
        if role.id == ORCHESTRATOR.id:
            planner_calls += 1
            if planner_calls == 1:
                assert run is not None
                run.control.fail_with(_StopRequestedError("stop requested"))
                return portfolio("alpha", "beta")
            assert '"hypothesis_id":"beta"' in message
            return {
                "reasoning": "Retry beta with the review fix; explore gamma.",
                "workstreams": [
                    *PortfolioPlan.model_validate(portfolio("gamma")).workstreams,
                    *PortfolioPlan.model_validate(
                        portfolio("beta", continue_hypothesis=True)
                    ).workstreams,
                ],
            }
        hypothesis_id = next(name for name in ("alpha", "beta", "gamma") if f"`{name}`" in message)
        if role.id == IMPLEMENTER.id:
            return implementation(hypothesis_id)
        rejected = hypothesis_id == "beta" and planner_calls == 1
        return {
            "passed": not rejected,
            "analysis": "Reviewed.",
            "feedback": "Breaks correctness." if rejected else "",
        }

    async def scenario() -> FakeRun:
        nonlocal run
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(domain_id="generic", objective="Improve.", benchmark_configured=True),
            responder=respond,
            supported_extra_tools={"evaluation", "profiler"},
            supports_parallel_candidates=True,
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
                AgentCapability.DURABLE_TURN_CONTINUATION,
            },
        )
        run.evaluation.script_benchmark(
            INPUT_BASELINE,
            *(
                BenchmarkEvaluation(
                    executed=True,
                    metric_name="throughput",
                    metric_value=value,
                    metric_direction=MetricDirection.MAXIMIZE,
                    row={"throughput": value},
                )
                for value in (10.0, 12.0, 15.0)
            ),
        )
        options = dynamic_options(max_rounds=2)
        with pytest.raises(_StopRequestedError):
            await PLUGIN.orchestrate(run, options)
        stopped = await run.state.load(DynamicState)
        assert stopped is not None
        assert stopped.next_planning_call == 2
        assert stopped.winner_revision is None
        run.control.fail_with(None)
        assert await PLUGIN.orchestrate(run, options) is RunStatus.SUCCEEDED
        return run

    finished = asyncio.run(scenario())
    assert planner_calls == 2
    # One input measurement, then one per evaluated candidate.
    assert len(finished.evaluation.benchmark_calls) == 1 + 3
    assert all(candidate.discarded for candidate in finished.workspaces.candidates)
    state = asyncio.run(finished.state.load(DynamicState))
    assert state is not None
    assert state.next_planning_call == 3
    assert [record.hypothesis_id for record in state.search.rounds].count("beta") == 2
    assert {record.hypothesis_id for record in state.search.rounds} == {"alpha", "beta", "gamma"}
    best = max(
        (record for record in state.search.rounds if record.perf_metric is not None),
        key=lambda record: record.perf_metric or 0.0,
    )
    assert best.perf_metric == 15.0
    assert state.winner_revision == best.commit
    assert state.adoption_pending is False
    assert finished.workspaces.root.restore_calls[-1][0] == state.winner_revision


def _finished_state(tmp_path: Path) -> DynamicState:
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("kept")],
            IMPLEMENTER.id: [implementation("kept")],
            JUDGE.id: [{"passed": True, "analysis": "Candidate is correct."}],
        }
    )

    async def scenario() -> DynamicState | None:
        run = baseline_run(tmp_path, script)
        run.evaluation.script_benchmark(INPUT_BASELINE, throughput(2.0))
        await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1))
        return await run.state.load(DynamicState)

    state = asyncio.run(scenario())
    assert state is not None
    return state


@settings(max_examples=20, deadline=None)
@given(
    counted=st.booleans(),
    due=st.booleans(),
    requested=st.booleans(),
    eligible=st.integers(min_value=0, max_value=50),
)
def test_state_written_before_the_retired_fields_were_removed_still_loads(
    tmp_path_factory: pytest.TempPathFactory,
    *,
    counted: bool,
    due: bool,
    requested: bool,
    eligible: int,
) -> None:
    current = _finished_state(tmp_path_factory.mktemp("run"))
    legacy = current.model_dump(mode="json")
    legacy["schema_version"] = 1
    legacy["next_epoch"] = legacy.pop("next_planning_call")
    legacy["eligible_evaluation_candidates"] = eligible
    for item in legacy["workstreams"]:
        item["epoch"] = item.pop("planning_call")
        budget = item.pop("budget")
        item["attempts"], item["refunded_attempts"] = budget["spent"], budget["refunded"]
        item.pop("implementer_started")
        item["member_id"] = item["hypothesis_id"]
        item["evaluation_eligibility_counted"] = counted
        item["cadence_evaluation_due"] = due
        item["plan"]["request_evaluation"] = requested

    assert DynamicState.model_validate_json(json.dumps(legacy)) == current


@pytest.mark.parametrize("version", [2, 3, 4])
def test_state_from_before_the_planning_call_rename_or_the_budget_still_loads(
    tmp_path: Path, version: int
) -> None:
    current = _finished_state(tmp_path)
    legacy = current.model_dump(mode="json")
    legacy["schema_version"] = version
    if version == 2:
        legacy["next_epoch"] = legacy.pop("next_planning_call")
    for item in legacy["workstreams"]:
        item.pop("implementer_started")
        if version == 2:
            item["epoch"] = item.pop("planning_call")
        if version < 4:
            budget = item.pop("budget")
            item["attempts"], item["refunded_attempts"] = budget["spent"], budget["refunded"]

    assert DynamicState.model_validate_json(json.dumps(legacy)) == current


def test_current_state_still_rejects_a_retired_key(tmp_path: Path) -> None:
    current = _finished_state(tmp_path).model_dump(mode="json")
    current["workstreams"][0]["member_id"] = current["workstreams"][0]["hypothesis_id"]
    with pytest.raises(ValidationError, match="member_id"):
        DynamicState.model_validate_json(json.dumps(current))


class _ProfilerStoppedError(RuntimeError):
    """Synthetic process stop while a profile runs."""


def test_an_interrupted_profile_runs_again_on_resume_without_replanning(tmp_path: Path) -> None:
    """A profile has no outcome until its operation ends; resume runs it, not the planner."""
    profile_plan = {
        "reasoning": "Measure before choosing a mechanism.",
        "workstreams": [
            {
                "kind": "profile",
                "profile_id": "prof-base",
                "target_hypothesis_id": None,
                "question": "Where does the time go?",
                "decision_impact": "Prioritize the implementation that removes the dominant cost.",
            }
        ],
    }
    script = Script({ORCHESTRATOR.id: [profile_plan]})

    async def scenario() -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(domain_id="llm-serving", objective="Improve.", profiler_id="rocprof"),
            responder=script.respond,
            supported_extra_tools={"evaluation", "profiler"},
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
                AgentCapability.DURABLE_TURN_CONTINUATION,
            },
            supports_parallel_candidates=True,
        )
        options = dynamic_options(max_in_flight=1)
        run.evaluation.profiling_supported = True
        run.evaluation.script_profile(
            _ProfilerStoppedError("stop"),
            CandidateProfile(
                revision="any",
                status=CandidateProfileStatus.OBSERVED,
                operation_id="op-1",
                diagnosis="Decode dominates.",
            ),
        )
        with pytest.raises(_ProfilerStoppedError):
            await PLUGIN.orchestrate(run, options)
        interrupted = await run.state.load(DynamicState)
        assert interrupted is not None
        assert interrupted.profiles[0].outcome is None
        await PLUGIN.orchestrate(run, options)
        return run

    run = asyncio.run(scenario())

    assert len([s for s in run.agents.sessions if s.role.id == ORCHESTRATOR.id]) == 1
    root = run.workspaces.root.revision
    assert [call.revision for call in run.evaluation.profile_calls] == [root, root]
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    (profile,) = state.profiles
    assert profile.outcome is not None
    assert profile.outcome.status is CandidateProfileStatus.OBSERVED
    assert profile.outcome.revision == root
    assert state.search.rounds == []
