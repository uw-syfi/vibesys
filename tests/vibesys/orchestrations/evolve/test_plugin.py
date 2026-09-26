"""Policy tests for the explicit evolutionary-search plugin."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from vibesys.evaluators.metrics import MetricSpace, Objective
from vibesys.orchestrations.evolve.models import EvolveOptions, EvolveState
from vibesys.orchestrations.evolve.plugin import PLUGIN
from vibesys.search.population import CandidateOutcome, PopulationConfig, PopulationSearch
from vs_runtime.api import AgentRole, RunFacts, RunStatus, RuntimeContractError
from vs_runtime.api.testing import FakeRunHost

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel


def _options(**updates: object) -> EvolveOptions:
    values: dict[str, object] = {
        "max_generations": 1,
        "children_per_generation": 1,
        "k_top_inspirations": 0,
        "k_random_inspirations": 0,
        "selection_temperature": 1.0,
        "frontier_bias": 0.7,
        "bootstrap_max_attempts": 1,
        "keep_deployments": False,
        "max_parallelism": 1,
    }
    values.update(updates)
    return EvolveOptions.model_validate(values)


def _passing_responder(
    role: AgentRole,
    _history: tuple[str, ...],
    _message: str,
    _response: type[BaseModel] | None,
) -> object:
    if role.id == "implementer":
        return {
            "summary": "reduced allocation overhead",
            "hypothesis": "reuse avoids repeated allocation",
            "expected_behavior": "lower latency",
        }
    if role.id == "judge":
        return {"analysis": "candidate is sound", "feedback": "", "verdict": "pass"}
    return {
        "analysis": "measured steady state",
        "bottlenecks": "allocation",
        "suggestions": "reuse buffers",
        "perf_metric": 100.0,
        "perf_unit": "tokens/s",
    }


def test_profiler_none_reuses_only_mutator_and_judge_sessions(tmp_path: Path) -> None:
    async def scenario() -> None:
        host = FakeRunHost(PLUGIN, project_root=tmp_path, responder=_passing_responder)
        status = await PLUGIN.orchestrate(host, _options())

        assert status is RunStatus.SUCCEEDED
        assert [session.role.id for session in host.agents.sessions] == ["implementer", "judge"]
        assert [len(session.history) for session in host.agents.sessions] == [2, 2]
        state = await host.state.load(EvolveState)
        assert state is not None
        assert len(state.population.individuals) == 2
        assert state.population.generation == 1
        await host.close()

    asyncio.run(scenario())


def test_parallel_candidates_get_isolated_explicit_profiler_sessions(tmp_path: Path) -> None:
    async def scenario() -> None:
        host = FakeRunHost(
            PLUGIN,
            project_root=tmp_path,
            responder=_passing_responder,
            supports_parallel_candidates=True,
            facts=RunFacts(
                domain_id="generic",
                objective="Increase throughput.",
                profiler_id="linux_cpu",
                benchmark_configured=True,
            ),
        )
        status = await PLUGIN.orchestrate(
            host,
            _options(children_per_generation=2, max_parallelism=2),
        )

        assert status is RunStatus.SUCCEEDED
        assert [candidate.id for candidate in host.workspaces.candidates] == [
            "candidate-1",
            "candidate-2",
        ]
        assert all(candidate.discarded for candidate in host.workspaces.candidates)
        profiler_sessions = [
            session for session in host.agents.sessions if session.role.id == "profiler"
        ]
        assert len(profiler_sessions) == 3
        assert all(len(session.history) == 1 for session in profiler_sessions)
        candidate_session_workspaces = {
            session.workspace.id for session in host.agents.sessions if session.workspace.id
        }
        assert candidate_session_workspaces == {"candidate-1", "candidate-2"}
        assert len(host.evaluation.accuracy_calls) == 3
        assert len(host.evaluation.benchmark_calls) == 3
        await host.close()

    asyncio.run(scenario())


def test_candidate_is_discarded_when_session_construction_fails(tmp_path: Path) -> None:
    async def scenario() -> None:
        host = FakeRunHost(
            PLUGIN,
            project_root=tmp_path,
            responder=_passing_responder,
            supports_parallel_candidates=True,
        )
        host.agents.script_creation(
            None,
            None,
            None,
            RuntimeContractError("candidate judge failed to open"),
        )

        with pytest.raises(RuntimeContractError, match="judge failed to open"):
            await PLUGIN.orchestrate(
                host,
                _options(max_parallelism=2),
            )

        assert len(host.workspaces.candidates) == 1
        assert host.workspaces.candidates[0].discarded
        await host.close()

    asyncio.run(scenario())


def test_parallel_failure_finishes_cleanup_for_every_created_candidate(tmp_path: Path) -> None:
    async def scenario() -> None:
        host = FakeRunHost(
            PLUGIN,
            project_root=tmp_path,
            responder=_passing_responder,
            supports_parallel_candidates=True,
        )
        host.agents.script_creation(
            None,
            None,
            None,
            None,
            None,
            RuntimeContractError("second candidate judge failed to open"),
        )

        with pytest.raises(RuntimeContractError, match="second candidate judge failed"):
            await PLUGIN.orchestrate(
                host,
                _options(children_per_generation=2, max_parallelism=2),
            )

        assert [candidate.id for candidate in host.workspaces.candidates] == [
            "candidate-1",
            "candidate-2",
        ]
        assert all(candidate.discarded for candidate in host.workspaces.candidates)
        await host.close()

    asyncio.run(scenario())


def test_judge_failure_skips_trusted_evaluation(tmp_path: Path) -> None:
    def failing_judge(
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        response: type[BaseModel] | None,
    ) -> object:
        if role.id == "judge":
            return {"analysis": "incorrect", "feedback": "tests fail", "verdict": "fail"}
        return _passing_responder(role, history, message, response)

    async def scenario() -> None:
        host = FakeRunHost(PLUGIN, project_root=tmp_path, responder=failing_judge)
        status = await PLUGIN.orchestrate(host, _options())

        assert status is RunStatus.FAILED
        assert host.evaluation.accuracy_calls == []
        assert host.evaluation.benchmark_calls == []
        await host.close()

    asyncio.run(scenario())


def test_projection_exposes_committed_population_and_metric_space() -> None:
    space = MetricSpace(objectives=(Objective(name="throughput", direction="max"),))
    population = PopulationSearch(PopulationConfig(space=space, seed=3)).initial()
    state = EvolveState(population=population, metric_space=space)

    assert PLUGIN.project is not None
    projection = PLUGIN.project(state)
    assert projection.payload is not None
    assert projection.payload["generation"] == 0
    assert projection.payload["metric_space"] == space.model_dump(mode="json")


def test_openevolve_exports_every_passing_revision_through_runtime(tmp_path: Path) -> None:
    async def scenario() -> None:
        host = FakeRunHost(PLUGIN, project_root=tmp_path, responder=_passing_responder)
        status = await PLUGIN.orchestrate(
            host,
            _options(search_policy="openevolve"),
        )

        assert status is RunStatus.SUCCEEDED
        assert len(host.workspaces.export_patch_calls) == 2
        state = await host.state.load(EvolveState)
        assert state is not None
        assert state.population.selector_state is not None
        await host.close()

    asyncio.run(scenario())


def test_resume_replays_proposals_but_skips_admitted_slots(tmp_path: Path) -> None:
    async def scenario() -> None:
        options = _options(children_per_generation=2)
        host = FakeRunHost(PLUGIN, project_root=tmp_path, responder=_passing_responder)
        search = PopulationSearch(
            PopulationConfig(
                space=options.metric_space,
                selection_temperature=options.selection_temperature,
                frontier_bias=options.frontier_bias,
                k_top_inspirations=options.k_top_inspirations,
                k_random_inspirations=options.k_random_inspirations,
                seed=options.seed,
            )
        )
        _, seeded = search.admit(
            search.initial(),
            CandidateOutcome(
                passed=True,
                parent_id=None,
                summary="seed",
                feedback="",
                commit=host.workspaces.root.revision,
            ),
        )
        generation_start = search.end_generation(seeded)
        first_proposal, _ = search.propose(generation_start)
        assert first_proposal is not None
        _, after_first = search.admit(
            generation_start,
            CandidateOutcome(
                passed=False,
                parent_id=first_proposal.parent.id,
                inspiration_ids=tuple(item.id for item in first_proposal.inspirations),
                summary="already evaluated",
                feedback="failed",
            ),
        )
        await host.state.commit(
            EvolveState(
                population=after_first,
                metric_space=options.metric_space,
                generation_start=generation_start,
                admitted_slots=1,
            ),
            workspace=host.workspaces.root,
            label="interrupted after slot one",
        )

        status = await PLUGIN.orchestrate(host, options)

        assert status is RunStatus.SUCCEEDED
        assert [len(session.history) for session in host.agents.sessions] == [1, 1]
        assert len(host.evaluation.accuracy_calls) == 1
        state = await host.state.load(EvolveState)
        assert state is not None
        assert len(state.population.individuals) == 3
        assert state.generation_start is None
        assert state.admitted_slots == 0
        await host.close()

    asyncio.run(scenario())
