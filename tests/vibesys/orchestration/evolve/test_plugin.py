"""Policy tests for the explicit evolutionary-search plugin."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from vibesys.orchestration.evolve.models import EvolveOptions, EvolveState
from vibesys.orchestration.evolve.plugin import PLUGIN
from vibesys.orchestration.evolve.population import (
    CandidateOutcome,
    PopulationConfig,
    PopulationSearch,
)
from vibesys.orchestration.metrics import MetricSpace, Objective
from vs_runtime.api import (
    AccuracyEvaluation,
    AgentRole,
    RunFacts,
    RunStatus,
    RuntimeContractError,
)
from vs_runtime.api.testing import FakeRunHost

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel


class _ParallelBatchInterruptedError(RuntimeError):
    """Deterministic interruption injected during one parallel batch."""


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


def test_deployment_retention_is_rejected_outside_the_runtime_boundary() -> None:
    with pytest.raises(ValidationError, match="keep_deployments"):
        _options(keep_deployments=True)


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


def test_trusted_accuracy_rejects_candidate_after_judge_passes(tmp_path: Path) -> None:
    async def scenario() -> None:
        host = FakeRunHost(
            PLUGIN,
            project_root=tmp_path,
            responder=_passing_responder,
            facts=RunFacts(
                domain_id="generic",
                objective="Increase throughput.",
                profiler_id="linux_cpu",
                benchmark_configured=True,
            ),
        )
        host.evaluation.script_accuracy(
            AccuracyEvaluation(
                executed=True,
                feedback="trusted accuracy rejected the candidate",
            )
        )

        status = await PLUGIN.orchestrate(host, _options())

        assert status is RunStatus.FAILED
        assert len(host.evaluation.accuracy_calls) == 1
        assert host.evaluation.benchmark_calls == []
        profiler = next(
            session for session in host.agents.sessions if session.role.id == "profiler"
        )
        assert profiler.history == ()
        state = await host.state.load(EvolveState)
        assert state is not None
        assert len(state.population.individuals) == 1
        assert state.population.individuals[0].passed is False
        assert state.population.individuals[0].feedback == "trusted accuracy rejected the candidate"
        await host.close()

    asyncio.run(scenario())


def test_parallelism_option_falls_back_to_serial_workspace(tmp_path: Path) -> None:
    async def scenario() -> None:
        host = FakeRunHost(
            PLUGIN,
            project_root=tmp_path,
            responder=_passing_responder,
            supports_parallel_candidates=False,
        )

        status = await PLUGIN.orchestrate(
            host,
            _options(children_per_generation=2, max_parallelism=2),
        )

        assert status is RunStatus.SUCCEEDED
        assert host.workspaces.candidates == ()
        assert [session.workspace.id for session in host.agents.sessions] == [None, None]
        assert [len(session.history) for session in host.agents.sessions] == [3, 3]
        state = await host.state.load(EvolveState)
        assert state is not None
        assert len(state.population.individuals) == 3
        await host.close()

    asyncio.run(scenario())


def test_interrupted_parallel_batch_reopens_atomically_and_cleans_up(tmp_path: Path) -> None:
    implementer_turn = 0

    def interrupt_once(
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        response: type[BaseModel] | None,
    ) -> object:
        nonlocal implementer_turn
        if role.id == "implementer":
            implementer_turn += 1
            if implementer_turn == 3:
                raise _ParallelBatchInterruptedError
        return _passing_responder(role, history, message, response)

    async def scenario() -> None:
        host = FakeRunHost(
            PLUGIN,
            project_root=tmp_path,
            responder=interrupt_once,
            supports_parallel_candidates=True,
        )
        options = _options(children_per_generation=2, max_parallelism=2)

        with pytest.raises(_ParallelBatchInterruptedError):
            await PLUGIN.orchestrate(host, options)

        interrupted = await host.state.load(EvolveState)
        assert interrupted is not None
        assert len(interrupted.population.individuals) == 1
        assert interrupted.generation_start is not None
        assert interrupted.admitted_slots == 0
        assert len(host.workspaces.candidates) == 2
        assert all(candidate.discarded for candidate in host.workspaces.candidates)
        interrupted_candidate_sessions = [
            session for session in host.agents.sessions if session.workspace.id is not None
        ]
        assert len(interrupted_candidate_sessions) == 4
        assert all(session.closed for session in interrupted_candidate_sessions)

        status = await PLUGIN.orchestrate(host, options)

        assert status is RunStatus.SUCCEEDED
        reopened = await host.state.load(EvolveState)
        assert reopened is not None
        assert len(reopened.population.individuals) == 3
        assert reopened.generation_start is None
        assert reopened.admitted_slots == 0
        assert len(host.workspaces.candidates) == 4
        assert all(candidate.discarded for candidate in host.workspaces.candidates)
        all_candidate_sessions = [
            session for session in host.agents.sessions if session.workspace.id is not None
        ]
        assert len(all_candidate_sessions) == 8
        assert all(session.closed for session in all_candidate_sessions)
        await host.close()

    asyncio.run(scenario())


def test_bootstrap_repairs_the_retained_wip_seed(tmp_path: Path) -> None:
    implementer_messages: list[str] = []
    judge_turn = 0

    def responder(
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        response: type[BaseModel] | None,
    ) -> object:
        nonlocal judge_turn
        if role.id == "implementer":
            implementer_messages.append(message)
            return _passing_responder(role, history, message, response)
        if role.id == "judge":
            judge_turn += 1
            if judge_turn == 1:
                return {
                    "analysis": "the endpoint is missing",
                    "feedback": "add the missing endpoint",
                    "verdict": "fail",
                }
        return _passing_responder(role, history, message, response)

    async def scenario() -> None:
        host = FakeRunHost(PLUGIN, project_root=tmp_path, responder=responder)
        status = await PLUGIN.orchestrate(host, _options(bootstrap_max_attempts=2))

        state = await host.state.load(EvolveState)
        assert status is RunStatus.SUCCEEDED
        assert state is not None
        failed, seed, child = state.population.individuals
        assert failed.passed is False
        assert failed.commit is not None
        assert seed.passed is True
        assert seed.commit is not None
        assert seed.commit != failed.commit
        assert child.parent_id == seed.id
        assert (
            "A previous bootstrap attempt's files are already in the workspace"
            in (implementer_messages[1])
        )
        assert "add the missing endpoint" in implementer_messages[1]
        await host.close()

    asyncio.run(scenario())


def test_pareto_metrics_keep_both_non_dominated_candidates(tmp_path: Path) -> None:
    space = MetricSpace(
        objectives=(
            Objective(name="throughput", direction="max"),
            Objective(name="latency_ms", direction="min"),
        )
    )
    profiler_results = iter(
        (
            {"throughput": 100.0, "latency_ms": 80.0},
            {"throughput": 80.0, "latency_ms": 50.0},
        )
    )

    def responder(
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        response: type[BaseModel] | None,
    ) -> object:
        if role.id != "profiler":
            return _passing_responder(role, history, message, response)
        metrics = next(profiler_results)
        return {
            "analysis": "measured both objectives",
            "bottlenecks": "tradeoff",
            "suggestions": "explore the frontier",
            "perf_metric": metrics["throughput"],
            "perf_unit": "requests/s",
            "metrics": metrics,
        }

    async def scenario() -> None:
        host = FakeRunHost(
            PLUGIN,
            project_root=tmp_path,
            responder=responder,
            facts=RunFacts(
                domain_id="generic",
                objective="Increase throughput without increasing latency.",
                profiler_id="linux_cpu",
            ),
        )
        status = await PLUGIN.orchestrate(host, _options(metric_space=space))

        state = await host.state.load(EvolveState)
        assert status is RunStatus.SUCCEEDED
        assert state is not None
        seed, child = state.population.individuals
        assert seed.metrics == {"throughput": 100.0, "latency_ms": 80.0}
        assert child.metrics == {"throughput": 80.0, "latency_ms": 50.0}
        search = PopulationSearch(PopulationConfig(space=space, seed=0))
        assert {item.id for item in search.frontier(state.population)} == {seed.id, child.id}
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
