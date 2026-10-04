"""The planner is offered profiles only while the run can produce profile evidence."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, cast

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._support import (
    INPUT_BASELINE,
    Script,
    dynamic_options,
    implementation,
    portfolio,
    throughput,
)

from vibesys.orchestration.dynamic import PLUGIN, DynamicPlanningError, DynamicState
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vs_runtime.api import (
    AgentCapability,
    CandidateProfile,
    CandidateProfileStatus,
    RunFacts,
    RunStatus,
)
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

    from vs_runtime.api import AgentRole

_PROFILE_PARAGRAPH = 'a profile workstream: `kind` "profile"'
_PASSED = {"passed": True, "analysis": "Candidate is correct."}


def _profile(identifier: str) -> dict[str, object]:
    return {
        "reasoning": "Measure before choosing a mechanism.",
        "workstreams": [
            {
                "kind": "profile",
                "profile_id": identifier,
                "target_hypothesis_id": None,
                "question": "Where does the time go?",
                "decision_impact": "Prioritize the implementation that removes the dominant cost.",
            }
        ],
    }


class _PlannerSchemas(Script):
    """A script that also records the reply schema of every planning turn."""

    def __init__(self, replies: dict[str, list[object]]) -> None:
        super().__init__(replies)
        self.schemas: list[str] = []

    def respond(
        self,
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        if role.id == ORCHESTRATOR.id and _response is not None:
            self.schemas.append(json.dumps(_response.model_json_schema()))
        return super().respond(role, history, message, _response)

    def planner_messages(self) -> list[str]:
        return [message for role, _, message in self.calls if role == ORCHESTRATOR.id]


def _profiled_run(tmp_path: Path, script: Script, *, profiler_id: str = "rocprof") -> FakeRun:
    """Return a run that provisions the ``profiler_id`` profiler agent."""
    run = FakeRun(
        PLUGIN,
        project_root=tmp_path,
        facts=RunFacts(
            domain_id="llm-serving",
            objective="Improve.",
            benchmark_configured=True,
            profiler_id=profiler_id,
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
    run.evaluation.script_root_benchmark(INPUT_BASELINE)
    return run


def test_a_run_whose_executor_cannot_profile_is_never_offered_a_profile(tmp_path: Path) -> None:
    """A provisioned profiler is not enough: the executor must produce profile evidence."""
    script = _PlannerSchemas(
        {
            ORCHESTRATOR.id: [_profile("prof-base"), portfolio("A")],
            IMPLEMENTER.id: [implementation("A")],
            JUDGE.id: [_PASSED],
        }
    )

    async def scenario() -> tuple[RunStatus, FakeRun]:
        run = _profiled_run(tmp_path, script)
        run.evaluation.script_benchmark(throughput(2.0))
        status = await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1))
        return status, run

    status, run = asyncio.run(scenario())

    assert status is RunStatus.SUCCEEDED
    assert run.evaluation.profile_calls == []
    first, correction = script.planner_messages()
    assert _PROFILE_PARAGRAPH not in first
    assert "profiling" not in first
    assert all('"profile"' not in schema for schema in script.schemas)
    assert "workstreams[0].kind: this run cannot produce trusted profile evidence" in correction
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    assert state.profiles == []
    assert [item.hypothesis_id for item in state.workstreams] == ["A"]


def test_an_unsupported_profile_stops_profiling_and_spends_no_budget(tmp_path: Path) -> None:
    """The first unsupported outcome ends profiling for the run and refunds its slot."""
    script = _PlannerSchemas(
        {
            ORCHESTRATOR.id: [
                _profile("prof-1"),
                _profile("prof-2"),
                portfolio("A"),
                portfolio("B"),
            ],
            IMPLEMENTER.id: [implementation("A"), implementation("B")],
            JUDGE.id: [_PASSED, _PASSED],
        }
    )

    async def scenario() -> tuple[RunStatus, FakeRun]:
        run = _profiled_run(tmp_path, script)
        run.evaluation.profiling_supported = True
        run.evaluation.script_profile(
            CandidateProfile(
                revision="any",
                status=CandidateProfileStatus.UNSUPPORTED,
                capture_started=False,
                operation_id="op-1",
                diagnosis="No capture route reaches a GPU in this run.",
            )
        )
        run.evaluation.script_benchmark(throughput(2.0), throughput(3.0))
        status = await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1, max_rounds=2))
        return status, run

    status, run = asyncio.run(scenario())

    assert status is RunStatus.SUCCEEDED
    assert len(run.evaluation.profile_calls) == 1
    offered, after_unsupported, correction, last = script.planner_messages()
    assert _PROFILE_PARAGRAPH in offered
    assert '"profile"' in script.schemas[0]
    assert _PROFILE_PARAGRAPH not in after_unsupported
    assert all('"profile"' not in schema for schema in script.schemas[1:])
    assert "workstreams[0].kind: this run cannot produce trusted profile evidence" in correction
    assert _PROFILE_PARAGRAPH not in last
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    (profile,) = state.profiles
    assert profile.outcome is not None
    assert profile.outcome.status is CandidateProfileStatus.UNSUPPORTED
    # A budget of two workstreams still runs two implement workstreams.
    assert [item.hypothesis_id for item in state.workstreams] == ["A", "B"]


@settings(max_examples=9)
@given(
    status=st.sampled_from(CandidateProfileStatus),
    capture_started=st.one_of(st.none(), st.booleans()),
)
@example(status=CandidateProfileStatus.UNSUPPORTED, capture_started=False)
@example(status=CandidateProfileStatus.UNSUPPORTED, capture_started=True)
@example(status=CandidateProfileStatus.FAILED, capture_started=True)
@example(status=CandidateProfileStatus.OBSERVED, capture_started=True)
def test_only_unsupported_before_capture_refunds_the_live_scheduling_budget(
    tmp_path_factory: pytest.TempPathFactory,
    *,
    status: CandidateProfileStatus,
    capture_started: bool | None,
) -> None:
    script = _PlannerSchemas(
        {
            ORCHESTRATOR.id: [_profile("prof"), portfolio("A"), portfolio("B")],
            IMPLEMENTER.id: [implementation("A"), implementation("B")],
            JUDGE.id: [_PASSED, _PASSED],
        }
    )

    async def scenario() -> DynamicState:
        run = _profiled_run(tmp_path_factory.mktemp("profile-refund"), script)
        run.evaluation.profiling_supported = True
        run.evaluation.script_profile(
            CandidateProfile(
                revision="any",
                status=status,
                capture_started=capture_started,
                diagnosis=None if status is CandidateProfileStatus.FAILED else "Inconclusive",
                failure="capture failed" if status is CandidateProfileStatus.FAILED else None,
            )
        )
        run.evaluation.script_benchmark(throughput(2.0), throughput(3.0))
        assert (
            await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1, max_rounds=2))
            is RunStatus.SUCCEEDED
        )
        state = await run.state.load(DynamicState)
        assert state is not None
        return state

    state = asyncio.run(scenario())
    refundable = status is CandidateProfileStatus.UNSUPPORTED and capture_started is False
    assert len(state.workstreams) == 1 + refundable
    assert state.unsupported_profiles(scope="budget") == refundable


def test_a_run_without_a_profiler_never_receives_a_schema_with_the_profile_kind(
    tmp_path: Path,
) -> None:
    """r17 (--profiler none): the planner's reply schema still offered profiles.

    The executor here could produce profile evidence; without a provisioned
    profiler the run still cannot profile, so neither the prompt nor the reply
    schema a strict-schema agent receives may contain the profile kind.
    """
    script = _PlannerSchemas(
        {
            ORCHESTRATOR.id: [portfolio("A")],
            IMPLEMENTER.id: [implementation("A")],
            JUDGE.id: [_PASSED],
        }
    )

    async def scenario() -> RunStatus:
        run = _profiled_run(tmp_path, script, profiler_id="none")
        run.evaluation.profiling_supported = True
        run.evaluation.script_benchmark(throughput(2.0))
        return await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1))

    assert asyncio.run(scenario()) is RunStatus.SUCCEEDED
    (schema,) = script.schemas
    assert '"profile"' not in schema
    assert "ProfilePlan" not in schema
    assert _PROFILE_PARAGRAPH not in script.planner_messages()[0]


def test_a_plan_with_no_valid_workstream_after_correction_fails_the_run(tmp_path: Path) -> None:
    """r17: every workstream was dropped and the run ended as a completed search.

    With nothing running and nothing valid to schedule, the run fails with the
    validation error the correction did not fix instead of reporting a search.
    """
    script = _PlannerSchemas({ORCHESTRATOR.id: [_profile("prof-1"), _profile("prof-2")]})

    run = _profiled_run(tmp_path, script, profiler_id="none")

    async def scenario() -> RunStatus:
        return await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1))

    with pytest.raises(DynamicPlanningError, match=r"workstreams\[0\]\.kind"):
        asyncio.run(scenario())

    assert len(script.planner_messages()) == 2
    state = asyncio.run(run.state.load(DynamicState))
    assert state is None or (state.workstreams == [] and state.profiles == [])


def test_r21_profile_without_measurement_intent_is_corrected_before_dispatch(
    tmp_path: Path,
) -> None:
    """A malformed profile is returned to the planner, which can choose implementation."""
    malformed = {
        "reasoning": "One slot implements the required exact-prefix cache.",
        "workstreams": [
            {
                "kind": "profile",
                "profile_id": "prefix_cache_correctness_and_reuse",
                "question": "Is this unexpectedly a profile kind?",
                "target_hypothesis_id": None,
            }
        ],
    }
    script = _PlannerSchemas(
        {
            ORCHESTRATOR.id: [malformed, portfolio("prefix-cache")],
            IMPLEMENTER.id: [{"summary": "No viable cache change.", "outcome": "disproven"}],
        }
    )
    run = _profiled_run(tmp_path, script)
    run.evaluation.profiling_supported = True

    async def scenario() -> RunStatus:
        return await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1))

    status = asyncio.run(scenario())

    assert status is RunStatus.SUCCEEDED
    first, correction = script.planner_messages()
    assert _PROFILE_PARAGRAPH in first
    assert "workstreams.0.profile.decision_impact" in correction
    assert "Field required" in correction
    assert run.evaluation.profile_calls == []
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    assert state.profiles == []
    assert [item.hypothesis_id for item in state.workstreams] == ["prefix-cache"]


def test_phase_question_cannot_return_aggregate_evidence_as_an_answer(tmp_path: Path) -> None:
    """r23: a historical prose phase request was silently answered by aggregate capture."""
    requested = _profile("phases")
    cast("list[dict[str, object]]", requested["workstreams"])[0]["question"] = (
        "Split prefill/decode timing and HIP API overhead."
    )
    script = _PlannerSchemas(
        {
            ORCHESTRATOR.id: [requested, portfolio("A"), portfolio("B")],
            IMPLEMENTER.id: [implementation("A"), implementation("B")],
            JUDGE.id: [_PASSED, _PASSED],
        }
    )

    async def scenario() -> FakeRun:
        run = _profiled_run(tmp_path, script)
        run.evaluation.profiling_supported = True
        run.evaluation.script_profile(
            CandidateProfile(
                revision="any",
                status=CandidateProfileStatus.OBSERVED,
                diagnosis="Aggregate kernels: GEMM 61%, attention 22%.",
            )
        )
        run.evaluation.script_benchmark(throughput(2.0), throughput(3.0))
        await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1, max_rounds=2))
        return run

    run = asyncio.run(scenario())
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    (profile,) = state.profiles
    assert profile.outcome is not None
    assert profile.outcome.status is CandidateProfileStatus.UNSUPPORTED
    assert {field.value for field in profile.outcome.missing_fields} == {
        "prefill_timing",
        "decode_timing",
    }
    assert len(run.evaluation.profile_calls) == 1
    assert _PROFILE_PARAGRAPH in script.planner_messages()[1]
    assert "missing_fields" in script.planner_messages()[1]
    assert "prefill_timing" in script.planner_messages()[1]
    assert len(run.evaluation.profile_results) == 1


def test_unsupported_phases_keep_hip_available_and_reject_an_identical_request(
    tmp_path: Path,
) -> None:
    """A missing phase field says nothing about the descriptor's supported API capture."""
    first = _profile("phase-1")
    repeat = _profile("phase-2")
    hip = _profile("hip")
    for request in (first, repeat):
        cast("list[dict[str, object]]", request["workstreams"])[0]["question"] = (
            "prefill/decode split"
        )
    cast("list[dict[str, object]]", hip["workstreams"])[0]["question"] = "HIP API timing"
    script = _PlannerSchemas(
        {
            ORCHESTRATOR.id: [first, repeat, hip, portfolio("A")],
            IMPLEMENTER.id: [implementation("A")],
            JUDGE.id: [_PASSED],
        }
    )

    async def scenario() -> FakeRun:
        run = _profiled_run(tmp_path, script)
        run.evaluation.profiling_supported = True
        run.evaluation.script_profile(
            CandidateProfile(
                revision="r",
                status=CandidateProfileStatus.OBSERVED,
                diagnosis="HIP API timing: launch 40ns",
            )
        )
        run.evaluation.script_benchmark(throughput(2.0))
        await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1, max_rounds=2))
        return run

    run = asyncio.run(scenario())
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    assert [item.profile_id for item in state.profiles] == ["phase-1", "hip"]
    assert [call.member_id for call in run.evaluation.profile_calls] == ["phase-1", "hip"]
    assert state.profiles[0].outcome is not None
    assert state.profiles[0].outcome.status is CandidateProfileStatus.UNSUPPORTED
    assert state.profiles[1].outcome is not None
    assert state.profiles[1].outcome.status is CandidateProfileStatus.OBSERVED
    correction = script.planner_messages()[2]
    assert "prior profile could not supply" in correction
    assert "prefill_timing" in correction
    assert "decode_timing" in correction
    assert _PROFILE_PARAGRAPH in correction
    assert [item.hypothesis_id for item in state.workstreams] == ["A"]
