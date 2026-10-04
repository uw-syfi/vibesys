"""Planner turns, role prompts, and the portfolio history the planner sees."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel, ValidationError
from tests.vibesys.orchestration.dynamic._support import (
    INPUT_BASELINE,
    Script,
    baseline_run,
    dynamic_options,
    implementation,
    portfolio,
    requested_slots,
    throughput,
    two_epoch_script,
)

from vibesys.hypothesis import HypothesisOutcome
from vibesys.orchestration.dynamic import (
    PLUGIN,
    DynamicState,
    PortfolioPlan,
    WorkstreamPlan,
)
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR, PROFILER
from vibesys.orchestration.dynamic.models import EvidenceReference
from vibesys.orchestration.dynamic.prompts import render_portfolio
from vs_evaluation.api import EvaluationAgentRole
from vs_evaluation.api.tools import evaluation_tool_names
from vs_runtime.api import (
    AgentCapability,
    BenchmarkEvaluation,
    MetricDirection,
    RunFacts,
    RunStatus,
    SkillFact,
    StructuredResponseError,
)
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.api import AgentRole


def test_options_and_portfolios_are_strict() -> None:
    with pytest.raises(ValidationError, match="max_in_flight"):
        dynamic_options(max_in_flight=0)
    with pytest.raises(ValidationError, match="unexpected"):
        dynamic_options(unexpected=True)
    with pytest.raises(ValidationError, match="profiler"):
        PortfolioPlan.model_validate({**portfolio("one"), "profiler": []})
    with pytest.raises(ValidationError, match="profiler"):
        DynamicState.model_validate({"profiler": []})


def test_blocked_hypothesis_is_not_reviewed_or_redispatched_with_the_same_task(
    tmp_path: Path,
) -> None:
    """A blocked attempt costs no judge turn, and its unchanged task is refused.

    Re-dispatching the task that blocked an implementer repeats the failure;
    the planner must change the task to remove the blocker (or drop it).
    """
    blocked = {
        "summary": "Blocked: `reference/model.py` is read-only.",
        "outcome": "blocked",
        "evidence": [],
    }
    changed = PortfolioPlan.model_validate(portfolio("kernel", continue_hypothesis=True))
    continued = changed.workstreams[0]
    assert isinstance(continued, WorkstreamPlan)
    continued.task = "Build the fast path in `engine/` instead."
    script = Script(
        {
            ORCHESTRATOR.id: [
                portfolio("kernel"),
                portfolio("kernel", continue_hypothesis=True),
                changed.model_dump(),
            ],
            IMPLEMENTER.id: [blocked, implementation("kernel")],
            JUDGE.id: [{"passed": True, "analysis": "Candidate is correct."}],
        }
    )

    async def scenario() -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(domain_id="generic", objective="Improve.", benchmark_configured=True),
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
            BenchmarkEvaluation(
                executed=True,
                metric_name="throughput",
                metric_value=12.0,
                metric_direction=MetricDirection.MAXIMIZE,
                row={"throughput": 12.0},
            ),
        )
        assert await PLUGIN.orchestrate(
            run, dynamic_options(max_in_flight=1, max_rounds=2, judge_every=1)
        ) is (RunStatus.SUCCEEDED)
        return run

    asyncio.run(scenario())
    roles = [role for role, _, _ in script.calls]
    assert roles == [
        ORCHESTRATOR.id,
        IMPLEMENTER.id,
        ORCHESTRATOR.id,
        ORCHESTRATOR.id,
        IMPLEMENTER.id,
        JUDGE.id,
    ]
    correction = script.calls[3][2]
    assert "Correction required" in correction
    assert "was blocked" in correction
    assert "Build the fast path in `engine/` instead." in script.calls[4][2]


def _schema_field_names(schema: object) -> set[str]:
    """Return every property name in a JSON Schema, nested definitions included."""
    if isinstance(schema, dict):
        names = set(schema.get("properties", {}))
        return names.union(*(_schema_field_names(value) for value in schema.values()))
    if isinstance(schema, list):
        return set().union(*(_schema_field_names(value) for value in schema))
    return set()


@pytest.mark.parametrize("profiling", [True, False])
@pytest.mark.parametrize("input_state", ["passing", "failing", "unmeasured"])
def test_planner_prompt_describes_the_reply_schema_and_no_other_fields(
    input_state: str, *, profiling: bool
) -> None:
    """The prompt and the reply schema describe one shape.

    A planner told to return "the portfolio JSON" without its field names
    wrapped the plan in an invented `findings` field and was rejected on the
    first try. The prompt now names each top-level field, and every
    identifier it puts in backticks is a schema field, a qualified evidence
    field, an offered tool, or an outcome value the history rows use.
    """
    schema = PortfolioPlan.model_json_schema()
    prompt = render_portfolio(
        capacity=2,
        in_flight=0,
        remaining=4,
        objective="Raise throughput.",
        environment_notes="",
        skills=(),
        root_revision="rev0",
        profiling=profiling,
        baseline='{"throughput":1.0}' if input_state == "passing" else "",
        input_failure={"reason": "preflight failed"} if input_state == "failing" else None,
        input_partial='{"name":"rate","value":1.0}' if input_state == "failing" else "",
        history="[]",
        buildable='[{"hypothesis_id":"cache"}]',
        older_ids="",
    )

    named = set(re.findall(r"`([^`]+)`", prompt))
    qualified_evidence = {f"evidence.{name}" for name in EvidenceReference.model_fields}
    tools = set(evaluation_tool_names(EvaluationAgentRole.RUN_OBSERVER, run_observer=True))
    allowed = (
        _schema_field_names(schema)
        | qualified_evidence
        | tools
        | {item.value for item in HypothesisOutcome}
        | {"rev0"}
    )
    assert named <= allowed, sorted(named - allowed)
    assert set(schema["required"]) <= named


def test_profiler_role_is_read_only_resumable_and_evaluation_enabled() -> None:
    assert PROFILER in PLUGIN.agents
    assert PROFILER.workspace_access.value == "read_only"
    assert tuple(tool.id for tool in PROFILER.extra_tools) == ("evaluation", "profiler")
    assert PROFILER.required_capabilities == frozenset(
        {
            AgentCapability.MCP_SERVERS,
            AgentCapability.SESSION_REUSE,
            AgentCapability.PROVIDER_SESSION_RESUME,
            AgentCapability.DURABLE_TURN_CONTINUATION,
        }
    )


def test_every_role_prompt_states_the_objective_environment_and_measurement_rule(
    tmp_path: Path,
) -> None:
    """Agents see the run's constraints inline, not behind a path they cannot read.

    The effective objective lives in run state that agent sandboxes hide, and
    the environment notes name read-only inputs and where trusted evaluation
    runs; without them agents edit read-only inputs and probe for local GPUs.
    """
    objective = "Raise throughput. Operator constraint: build the engine in `engine/`."
    notes = "These inputs are read-only and edits to them fail: `reference`."
    hidden_location = ".vibesys/state/runs/r1/runtime/effective-objective.md"
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("engine")],
            IMPLEMENTER.id: [implementation("engine")],
            JUDGE.id: [{"passed": True, "analysis": "Candidate is correct."}],
        }
    )

    async def scenario() -> None:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(
                domain_id="generic",
                objective=objective,
                environment_notes=notes,
                objective_location=hidden_location,
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
        await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1))

    asyncio.run(scenario())
    prompts = {role: message for role, _, message in script.calls}
    assert set(prompts) == {ORCHESTRATOR.id, IMPLEMENTER.id, JUDGE.id}
    for prompt in prompts.values():
        assert objective in prompt
        assert notes in prompt
        assert hidden_location not in prompt
        assert "only the framework's trusted evaluation produces performance" in prompt
        assert "use python3 for Python code" in prompt
        assert "bash cpu_check/run.sh" in prompt
    assert "never assign edits to read-only inputs" in prompts[ORCHESTRATOR.id]
    assert "`submit_evaluation`" in prompts[IMPLEMENTER.id]
    # Trusted evaluation checks accuracy and speed, not the objective's other
    # rules (a forbidden dependency, a numerics policy); the judge enforces them.
    assert "reject a candidate that violates any rule or constraint the objective states" in (
        " ".join(prompts[JUDGE.id].split())
    )


@pytest.mark.parametrize(
    "skills",
    [
        (),
        (
            SkillFact(name="queue-tuning", description="Tune request queues under load."),
            SkillFact(name="cache-notes", description="Design notes for result caches."),
        ),
    ],
)
def test_every_role_prompt_names_the_offered_skills(
    tmp_path: Path, skills: tuple[SkillFact, ...]
) -> None:
    """Agents see each offered skill by name and description, and none when none is offered.

    A generic pointer to "installed skills" did not lead agents to load them.
    """
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("engine")],
            IMPLEMENTER.id: [implementation("engine")],
            JUDGE.id: [{"passed": True, "analysis": "Candidate is correct."}],
        }
    )

    async def scenario() -> None:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(
                domain_id="generic",
                objective="Improve.",
                benchmark_configured=True,
                skills=skills,
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
        await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1))

    asyncio.run(scenario())
    prompts = {role: message for role, _, message in script.calls}
    assert set(prompts) == {ORCHESTRATOR.id, IMPLEMENTER.id, JUDGE.id}
    for prompt in prompts.values():
        assert ("Skills installed for this run" in prompt) == bool(skills)
        for skill in skills:
            assert f"- {skill.name}: {skill.description}" in prompt


def test_portfolio_history_omits_large_evaluation_feedback(tmp_path: Path) -> None:
    diagnostic = "benchmark failure\n" * 10_000
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("failed"), portfolio("terminal")],
            IMPLEMENTER.id: [
                implementation("failed"),
                {"summary": "No viable follow-up.", "outcome": "disproven"},
            ],
            JUDGE.id: [{"passed": True, "analysis": "Candidate is correct."}],
        }
    )

    async def scenario() -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(domain_id="generic", objective="Improve.", benchmark_configured=True),
            responder=script.respond,
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
            INPUT_BASELINE, BenchmarkEvaluation(executed=True, feedback=diagnostic)
        )
        await PLUGIN.orchestrate(run, dynamic_options(max_rounds=2, max_in_flight=1))
        return run

    asyncio.run(scenario())
    orchestrator_messages = [
        message for role, _, message in script.calls if role == ORCHESTRATOR.id
    ]
    assert len(orchestrator_messages) == 2
    assert diagnostic not in orchestrator_messages[1]
    assert len(orchestrator_messages[1]) < 10_000


def test_planner_sees_every_used_hypothesis_id_beyond_the_history_window(
    tmp_path: Path,
) -> None:
    """IDs that scrolled out of the bounded history are still listed as used.

    The planner must not reuse an ID; a rejected portfolio costs a correction
    turn, and two rejections fail the run.
    """
    epochs = 10

    def respond(
        role: AgentRole,
        _history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        if role.id == ORCHESTRATOR.id:
            call = len(calls)
            return portfolio(
                *(f"c{call}-{slot}" for slot in range(requested_slots(message))),
            )
        return {"summary": "No viable change.", "outcome": "disproven"}

    calls: list[str] = []

    def recording(
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        response: type[BaseModel] | None,
    ) -> object:
        if role.id == ORCHESTRATOR.id:
            calls.append(message)
        return respond(role, history, message, response)

    async def scenario() -> None:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            responder=recording,
            supported_extra_tools={"evaluation", "profiler"},
            supports_parallel_candidates=True,
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
                AgentCapability.DURABLE_TURN_CONTINUATION,
            },
        )
        await PLUGIN.orchestrate(run, dynamic_options(max_rounds=epochs, judge_every=100))

    asyncio.run(scenario())
    assert len(calls) >= epochs
    assert "c1-0" in calls[-1]


def test_portfolio_history_explains_why_a_workstream_failed(tmp_path: Path) -> None:
    """The planner sees what was tried and why the review rejected it."""
    summary = "Raised the decode batch size to 32."
    feedback = "The change disables graph capture on the decode path."
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("rejected"), portfolio("next")],
            IMPLEMENTER.id: [
                {**implementation("rejected"), "summary": summary},
                {"summary": "No viable change.", "outcome": "disproven"},
            ],
            JUDGE.id: [{"passed": False, "analysis": "Incorrect.", "feedback": feedback}],
        }
    )

    async def scenario() -> None:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(domain_id="generic", objective="Improve.", benchmark_configured=True),
            responder=script.respond,
            supported_extra_tools={"evaluation", "profiler"},
            supports_parallel_candidates=True,
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
                AgentCapability.DURABLE_TURN_CONTINUATION,
            },
        )
        await PLUGIN.orchestrate(run, dynamic_options(max_rounds=2, max_in_flight=1))

    asyncio.run(scenario())
    planner_messages = [message for role, _, message in script.calls if role == ORCHESTRATOR.id]
    assert len(planner_messages) == 2
    assert summary in planner_messages[1]
    assert feedback in planner_messages[1]


def test_unparseable_agent_replies_are_corrected_in_the_same_session(tmp_path: Path) -> None:
    """One malformed structured reply costs a follow-up turn, not the run or an attempt."""
    script = Script(
        {
            ORCHESTRATOR.id: [
                StructuredResponseError(ORCHESTRATOR.id, PortfolioPlan),
                portfolio("recover"),
            ],
            IMPLEMENTER.id: [
                StructuredResponseError(IMPLEMENTER.id, BaseModel),
                implementation("recover"),
            ],
            JUDGE.id: [
                StructuredResponseError(JUDGE.id, BaseModel),
                {"passed": True, "analysis": "Candidate is correct."},
            ],
        }
    )

    async def scenario() -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(domain_id="generic", objective="Improve.", benchmark_configured=True),
            responder=script.respond,
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
        status = await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1))
        assert status is RunStatus.SUCCEEDED
        return run

    run = asyncio.run(scenario())
    for role in (ORCHESTRATOR, IMPLEMENTER, JUDGE):
        sessions = [session for session in run.agents.sessions if session.role.id == role.id]
        assert len(sessions) == 1
        messages = [message for role_id, _, message in script.calls if role_id == role.id]
        assert len(messages) == 2
        assert messages[1].startswith("Correction required")
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    assert state.workstreams[0].budget.spent == 1
    assert state.workstreams[0].phase.value == "evaluated"
    assert state.winner_revision == state.workstreams[0].candidate_revision


def test_planner_history_shows_that_an_accepted_candidate_was_discarded(tmp_path: Path) -> None:
    """A candidate that passes every gate but does not beat the input is not a success."""
    script = two_epoch_script()

    async def scenario() -> None:
        fake = baseline_run(tmp_path, script)
        fake.evaluation.script_root_benchmark(throughput(20.0))
        fake.evaluation.script_benchmark(throughput(12.0), throughput(25.0))
        run = fake
        await PLUGIN.orchestrate(run, dynamic_options(max_rounds=2, max_in_flight=1))

    asyncio.run(scenario())

    plans = [message for role, _, message in script.calls if role == ORCHESTRATOR.id]
    assert '"accepted":true' in plans[1]
    assert '"disposition":"discard"' in plans[1]


@pytest.mark.parametrize(
    ("correction", "later", "scheduled"),
    # Insisting leaves budget for one more workstream, planned when "a" ends.
    [(("a", "b"), (), ["a", "b"]), (("a",), ("c",), ["a", "c"])],
    ids=["fills", "insists"],
)
def test_planner_is_asked_once_to_fill_a_slot_it_left_free(
    tmp_path: Path,
    correction: tuple[str, ...],
    later: tuple[str, ...],
    scheduled: list[str],
) -> None:
    """r7: a one-workstream plan left the second slot idle for a 20-minute turn.

    The planner is asked once to fill the free slot; a planner that still finds
    no independent work keeps its smaller plan instead of failing the run.
    """
    script = Script(
        {
            ORCHESTRATOR.id: [
                portfolio("a"),
                portfolio(*correction),
                *([portfolio(*later)] if later else []),
            ],
            IMPLEMENTER.id: [implementation(name) for name in scheduled],
            JUDGE.id: [{"passed": True, "analysis": "Candidate is correct."} for _ in scheduled],
        }
    )

    async def scenario() -> tuple[RunStatus, DynamicState | None]:
        run = baseline_run(tmp_path, script)
        status = await PLUGIN.orchestrate(run, dynamic_options(max_rounds=1, max_in_flight=2))
        return status, await run.state.load(DynamicState)

    status, state = asyncio.run(scenario())

    assert status is RunStatus.SUCCEEDED
    planner = [message for role, _, message in script.calls if role == ORCHESTRATOR.id]
    assert "schedules 1 of 2 free slots" in planner[1]
    assert state is not None
    assert [item.hypothesis_id for item in state.workstreams] == scheduled


@pytest.mark.parametrize("role", [IMPLEMENTER, JUDGE])
def test_evaluation_suspension_roles_require_durable_same_session_resume(role: AgentRole) -> None:
    assert AgentCapability.PROVIDER_SESSION_RESUME in role.required_capabilities
    assert AgentCapability.DURABLE_TURN_CONTINUATION in role.required_capabilities
    assert AgentCapability.SESSION_REUSE in role.required_capabilities
