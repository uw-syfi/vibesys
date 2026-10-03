"""The planner is offered profiles only while the run can produce profile evidence."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
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
