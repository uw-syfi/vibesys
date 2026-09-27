"""Persisted restart and normalized trajectory tests for explicit evolve."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestrations.evolve._integration_support import (
    InterruptedTurnError,
    execute,
    load_state,
    options,
    write_input,
)

from vibesys.orchestrations.evolve.models import JudgeResponse, MutatorResponse
from vibesys.schemas import Verdict
from vs_agent.api import AgentCapabilities
from vs_agent.api.testing import FakeAgentClient
from vs_project.api import Project
from vs_runtime.api import RunStatus

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from pydantic import BaseModel

    from vs_agent.api.testing import FakeInvocation

    type _ResponseSource = (
        BaseModel | dict[str, object] | Callable[[FakeInvocation], BaseModel | dict[str, object]]
    )


_CAPABILITIES = AgentCapabilities(session_reuse=True, provider_session_resume=True)


@dataclass(frozen=True, slots=True)
class _ReopenScenario:
    name: str
    children: int
    crashed_responses: Sequence[_ResponseSource]
    crashed_values: Sequence[int | None]
    judge_count: int
    resume_values: Sequence[int]


def _mutation(summary: str) -> MutatorResponse:
    return MutatorResponse(
        summary=summary,
        hypothesis="Removing repeated work increases throughput.",
        expected_behavior="The queue processes more requests.",
    )


def _pass() -> JudgeResponse:
    return JudgeResponse(analysis="The candidate is correct.", feedback="", verdict=Verdict.PASS)


def _interrupt(_invocation: FakeInvocation) -> BaseModel | dict[str, object]:
    raise InterruptedTurnError("mutator")


def _mutator_client(
    responses: Sequence[_ResponseSource],
    *,
    written_values: Sequence[int | None],
    observed_inputs: list[int] | None = None,
) -> FakeAgentClient:
    client = FakeAgentClient(backend_name="stub", capabilities=_CAPABILITIES)
    client.enqueue("implementer", *responses)

    def write(invocation: FakeInvocation) -> None:
        index = len(client.calls_for("implementer")) - 1
        if observed_inputs is not None:
            current = (invocation.workspace / "queue.py").read_text(encoding="utf-8")
            observed_inputs.append(int(current.removeprefix("VALUE = ").strip()))
        value = written_values[index]
        if value is not None:
            (invocation.workspace / "queue.py").write_text(f"VALUE = {value}\n", encoding="utf-8")

    client.on_invoke(write)
    return client


def _judge_client(count: int) -> FakeAgentClient:
    return FakeAgentClient(backend_name="stub", capabilities=_CAPABILITIES).enqueue(
        "judge", *(_pass() for _ in range(count))
    )


def _verdicts(*verdicts: Verdict) -> FakeAgentClient:
    responses = [
        JudgeResponse(
            analysis="The candidate was reviewed.",
            feedback="" if verdict is Verdict.PASS else "Candidate violates the contract.",
            verdict=verdict,
        )
        for verdict in verdicts
    ]
    return FakeAgentClient(backend_name="stub", capabilities=_CAPABILITIES).enqueue(
        "judge", *responses
    )


def _clients(values: Sequence[int]) -> list[FakeAgentClient]:
    return [
        _mutator_client(
            [_mutation(f"candidate {value}") for value in values],
            written_values=values,
        ),
        _judge_client(len(values)),
    ]


def _interrupted_workspace(input_root: Path) -> tuple[Path, str]:
    projects = list((input_root.parent / f"{input_root.name}-runs").iterdir())
    assert len(projects) == 1
    workspace = projects[0]
    return workspace, Project.open(workspace).state.resolve_run().run_id


def _summary(project_root: Path, run_id: str) -> tuple[object, ...]:
    state = load_state(project_root, run_id)
    assert state is not None
    return (
        state.population.generation,
        state.generation_start,
        state.admitted_slots,
        tuple(
            (
                item.id,
                item.generation,
                item.parent_id,
                item.passed,
                item.summary,
                item.feedback,
                item.perf_metric,
                item.metrics,
            )
            for item in state.population.individuals
        ),
    )


@pytest.mark.parametrize(
    "scenario",
    [
        _ReopenScenario(
            name="pre_evaluation",
            children=1,
            crashed_responses=[_interrupt],
            crashed_values=[None],
            judge_count=0,
            resume_values=[2, 3],
        ),
        _ReopenScenario(
            name="post_bootstrap_admit",
            children=1,
            crashed_responses=[_mutation("candidate 2"), _interrupt],
            crashed_values=[2, None],
            judge_count=1,
            resume_values=[3],
        ),
        _ReopenScenario(
            name="partial_generation",
            children=2,
            crashed_responses=[_mutation("candidate 2"), _mutation("candidate 3"), _interrupt],
            crashed_values=[2, 3, None],
            judge_count=2,
            resume_values=[4],
        ),
    ],
    ids=lambda scenario: scenario.name,
)
def test_persisted_reopen_matches_uninterrupted_trajectory(
    tmp_path: Path,
    scenario: _ReopenScenario,
) -> None:
    configured = options(children_per_generation=scenario.children)
    all_values = list(range(2, scenario.children + 3))
    baseline_input = write_input(tmp_path / f"baseline-{scenario.name}")
    baseline_status, baseline_id, baseline_workspace = execute(
        baseline_input,
        _clients(all_values),
        configured=configured,
    )

    crashed_input = write_input(tmp_path / f"crashed-{scenario.name}")
    crashed_clients = [
        _mutator_client(scenario.crashed_responses, written_values=scenario.crashed_values),
        _judge_client(scenario.judge_count),
    ]
    with pytest.raises(InterruptedTurnError, match="mutator"):
        execute(crashed_input, crashed_clients, configured=configured)

    workspace, interrupted_id = _interrupted_workspace(crashed_input)
    resumed_clients = _clients(scenario.resume_values)
    resumed_status, resumed_id, resumed_workspace = execute(
        workspace,
        resumed_clients,
        configured=configured,
        resume_run_id=interrupted_id,
    )

    assert baseline_status is resumed_status is RunStatus.SUCCEEDED
    assert resumed_id == interrupted_id
    assert _summary(baseline_workspace, baseline_id) == _summary(resumed_workspace, resumed_id)
    state = load_state(resumed_workspace, resumed_id)
    assert state is not None
    assert state.generation_start is None
    assert state.admitted_slots == 0


def test_explicit_evolve_pass_trajectory_has_stable_durable_output(tmp_path: Path) -> None:
    input_root = write_input(tmp_path / "golden-input")
    status, run_id, workspace = execute(input_root, _clients([2, 3]))
    state = load_state(workspace, run_id)
    assert state is not None
    expected = json.loads(
        (Path(__file__).parent / "fixtures" / "pass_trajectory.json").read_text(encoding="utf-8")
    )
    actual = {
        "status": status.value,
        "generation": state.population.generation,
        "individuals": [
            {
                "id": item.id,
                "generation": item.generation,
                "parent_id": item.parent_id,
                "passed": item.passed,
                "summary": item.summary,
            }
            for item in state.population.individuals
        ],
        "cursor": {
            "generation_start": state.generation_start,
            "admitted_slots": state.admitted_slots,
        },
        "workspace": {"queue.py": (workspace / "queue.py").read_text(encoding="utf-8")},
    }
    assert actual == expected


def test_failed_child_is_restored_to_parent_before_next_candidate(tmp_path: Path) -> None:
    input_root = write_input(tmp_path / "restore-input")
    observed_inputs: list[int] = []
    mutator = _mutator_client(
        [_mutation("seed"), _mutation("rejected"), _mutation("accepted")],
        written_values=[2, 3, 4],
        observed_inputs=observed_inputs,
    )
    status, run_id, workspace = execute(
        input_root,
        [mutator, _verdicts(Verdict.PASS, Verdict.FAIL, Verdict.PASS)],
        configured=options(children_per_generation=2),
    )

    state = load_state(workspace, run_id)
    assert status is RunStatus.SUCCEEDED
    assert state is not None
    seed, rejected, accepted = state.population.individuals
    assert observed_inputs == [1, 2, 2]
    assert rejected.passed is False
    assert rejected.commit is None
    assert accepted.parent_id == seed.id
    assert (workspace / "queue.py").read_text(encoding="utf-8") == "VALUE = 4\n"


def test_final_workspace_uses_deterministic_latest_best_on_tie(tmp_path: Path) -> None:
    input_root = write_input(tmp_path / "selection-input")
    status, run_id, workspace = execute(
        input_root,
        _clients([2, 3, 4]),
        configured=options(children_per_generation=2),
    )

    state = load_state(workspace, run_id)
    assert status is RunStatus.SUCCEEDED
    assert state is not None
    assert all(item.perf_metric is None for item in state.population.individuals)
    assert (workspace / "queue.py").read_text(encoding="utf-8") == "VALUE = 4\n"
