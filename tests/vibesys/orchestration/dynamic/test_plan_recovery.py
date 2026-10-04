"""Dynamic planning survives slots that give up and plans the planner cannot correct."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING

import pytest

from vibesys.orchestration.dynamic import PLUGIN, DynamicOptions, DynamicPlanningError, DynamicState
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, ORCHESTRATOR
from vs_runtime.api import AgentCapability, AgentTurnTimeoutError, Run, RunFacts, RunStatus
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pydantic import BaseModel

    from vs_runtime.api import AgentRole, CandidateWorkspace, Workspace, Workspaces


class _ImplementerTransportError(RuntimeError):
    """Synthetic implementer failure: the turn never returns a result."""


def _options(**changes: object) -> DynamicOptions:
    return DynamicOptions.model_validate(
        {
            "interface": "service",
            "max_rounds": 2,
            "max_retries_per_round": 1,
            "judge_every": 3,
            "official_eval_every": 1,
            "max_in_flight": 1,
            "metric_space": {"objectives": [{"name": "throughput", "direction": "max"}]},
            **changes,
        }
    )


def _workstream(identifier: str, *, continue_hypothesis: bool = False) -> dict[str, object]:
    return {
        "hypothesis_id": identifier,
        "title": f"Investigate {identifier}",
        "hypothesis": f"Mechanism {identifier} limits the objective.",
        "task": f"Implement and verify {identifier}.",
        "pass_criteria": "The change is correct and measurably improves the objective.",
        "continue_hypothesis": continue_hypothesis,
    }


def _plan(*workstreams: dict[str, object], abandon: tuple[str, ...] = ()) -> dict[str, object]:
    return {
        "reasoning": "Explore independent limiting mechanisms.",
        "workstreams": list(workstreams),
        "hypothesis_updates": [
            {
                "hypothesis_id": identifier,
                "disposition": "abandoned",
                "reason_kind": "lower_priority",
                "reason": "It failed.",
            }
            for identifier in abandon
        ],
    }


class _Script:
    def __init__(self, replies: dict[str, list[object]]) -> None:
        self._replies = {role: deque(values) for role, values in replies.items()}
        self.planner_messages: list[str] = []

    def respond(
        self,
        role: AgentRole,
        _history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        if role.id == ORCHESTRATOR.id:
            self.planner_messages.append(message)
        reply = self._replies[role.id].popleft()
        if isinstance(reply, BaseException):
            raise reply
        return reply


class _SetupError(RuntimeError):
    """Synthetic failure raised while opening a candidate workspace."""


class _FailingWorkspaces:
    """Workspaces whose candidate creation raises the next scripted error."""

    def __init__(self, delegate: Workspaces, errors: list[str]) -> None:
        self.delegate = delegate
        self.errors = deque(errors)
        self.creations = 0

    @property
    def root(self) -> Workspace:
        return self.delegate.root

    @property
    def supports_parallel_candidates(self) -> bool:
        return self.delegate.supports_parallel_candidates

    async def create_candidate(
        self,
        from_revision: str | None = None,
        *,
        member_id: str | None = None,
    ) -> CandidateWorkspace:
        self.creations += 1
        if self.errors:
            raise _SetupError(self.errors.popleft())
        return await self.delegate.create_candidate(from_revision, member_id=member_id)

    async def adopt(self, revision: str) -> None:
        await self.delegate.adopt(revision)

    async def export_patch(self, revision: str) -> str:
        return await self.delegate.export_patch(revision)


def _fake(tmp_path: Path, script: _Script) -> FakeRun:
    return FakeRun(
        PLUGIN,
        project_root=tmp_path,
        facts=RunFacts(domain_id="generic", objective="Improve."),
        responder=script.respond,
        supported_extra_tools={"evaluation", "profiler"},
        supports_parallel_candidates=True,
        supported_agent_capabilities={
            AgentCapability.MCP_SERVERS,
            AgentCapability.SESSION_REUSE,
            AgentCapability.PROVIDER_SESSION_RESUME,
        },
    )


def _execute(
    tmp_path: Path,
    script: _Script,
    options: DynamicOptions,
    workspaces: Callable[[Workspaces], Workspaces] = lambda delegate: delegate,
) -> tuple[RunStatus, DynamicState]:
    async def scenario() -> tuple[RunStatus, DynamicState | None]:
        fake = _fake(tmp_path, script)
        run = Run(
            run_id=fake.run_id,
            facts=fake.facts,
            agents=fake.agents,
            workspaces=workspaces(fake.workspaces),
            evaluation=fake.evaluation,
            state=fake.state,
            control=fake.control,
            commands=fake.commands,
            skills=fake.skills,
            observations=fake.observations,
        )
        status = await PLUGIN.orchestrate(run, options)
        return status, await fake.state.load(DynamicState)

    status, state = asyncio.run(scenario())
    assert state is not None
    return status, state


def test_planner_may_abandon_a_hypothesis_whose_slot_gave_up(tmp_path: Path) -> None:
    """r8: a slot gave up before any implementer turn returned, and the run then
    died on the planner's (correct) decision to abandon that hypothesis.

    The given-up slot records an ``implementation_failed`` round, so the
    hypothesis is complete and the abandonment applies on the first plan.
    """
    script = _Script(
        {
            ORCHESTRATOR.id: [
                _plan(_workstream("H1")),
                _plan(_workstream("H3"), abandon=("H1",)),
            ],
            IMPLEMENTER.id: [
                _ImplementerTransportError("implementer transport failed"),
                {"summary": "No viable change.", "outcome": "disproven"},
            ],
        }
    )

    status, state = _execute(tmp_path, script, _options())

    assert status is RunStatus.SUCCEEDED
    assert len(script.planner_messages) == 2
    # The planner is told why H1 failed instead of a bare `failed`.
    assert "implementer transport failed" in script.planner_messages[1]
    assert not any("Correction required" in message for message in script.planner_messages)
    rounds = {record.hypothesis_id: record for record in state.search.rounds}
    assert rounds["H1"].hypothesis_outcome == "implementation_failed"
    strategies = {item.hypothesis_id: item.strategy for item in state.search.hypotheses}
    assert strategies["H1"].value == "abandoned"
    assert [item.hypothesis_id for item in state.workstreams] == ["H1", "H3"]


@pytest.mark.parametrize(
    ("invalid", "scheduled"),
    [
        # An update naming an unknown hypothesis is dropped; the workstream runs.
        (_plan(_workstream("a"), abandon=("ghost",)), ["a"]),
        # r19, r20: a repeated ID keeps its first entry; the repeat is dropped.
        (_plan(_workstream("a"), _workstream("a")), ["a"]),
    ],
    ids=["drops-update", "drops-repeated-id"],
)
def test_plan_still_invalid_after_correction_keeps_its_valid_part(
    tmp_path: Path, invalid: dict[str, object], scheduled: list[str]
) -> None:
    """A planner validation error is sent back once; it never ends the run."""
    script = _Script(
        {
            ORCHESTRATOR.id: [invalid, invalid],
            IMPLEMENTER.id: [{"summary": "No viable change.", "outcome": "disproven"}],
        }
    )

    status, state = _execute(tmp_path, script, _options(max_rounds=1))

    assert status is RunStatus.SUCCEEDED
    assert script.planner_messages[1].count("Correction required") == 1
    assert [item.hypothesis_id for item in state.workstreams] == scheduled
    assert all(item.strategy.value == "available" for item in state.search.hypotheses)


def test_a_plan_left_with_no_workstream_and_nothing_running_fails_the_run(
    tmp_path: Path,
) -> None:
    """r17: dropping every workstream with none in flight is not a finished search."""
    invalid = _plan(_workstream("ghost", continue_hypothesis=True))
    script = _Script({ORCHESTRATOR.id: [invalid, invalid], IMPLEMENTER.id: []})

    with pytest.raises(
        DynamicPlanningError, match=r"unknown hypothesis 'ghost' cannot be continued"
    ):
        _execute(tmp_path, script, _options(max_rounds=1))

    assert script.planner_messages[1].count("Correction required") == 1


@pytest.mark.parametrize(
    ("errors", "creations"),
    [
        # r8: the same setup error twice is deterministic; the third retry is skipped.
        (["invalid namespace 'm-H1'"] * 3, 2),
        # Distinct setup errors may be transient; each spends a retry (C2).
        (["index.lock exists", "disk full", "index.lock exists"], 3),
    ],
    ids=["repeated", "distinct"],
)
def test_repeated_setup_failure_gives_up_without_spending_every_retry(
    tmp_path: Path, errors: list[str], creations: int
) -> None:
    script = _Script({ORCHESTRATOR.id: [_plan(_workstream("h1"))], IMPLEMENTER.id: []})
    failing: list[_FailingWorkspaces] = []

    def wrap(delegate: Workspaces) -> Workspaces:
        failing.append(_FailingWorkspaces(delegate, errors))
        return failing[0]

    status, state = _execute(
        tmp_path, script, _options(max_rounds=1, max_retries_per_round=3), wrap
    )

    assert status is RunStatus.SUCCEEDED
    assert failing[0].creations == creations
    assert state.workstreams[0].phase.value == "failed"
    assert state.workstreams[0].last_error == errors[creations - 1]
    assert [record.hypothesis_outcome for record in state.search.rounds] == [
        "implementation_failed"
    ]


class _PlannerCrashError(RuntimeError):
    """Synthetic planner failure: the agent CLI exited mid-turn."""


# One faulted planning turn, as the planner's replies to its turn and correction.
_TURN_FAULTS: dict[str, Callable[[], list[object]]] = {
    "crash": lambda: [_PlannerCrashError("planner died")],
    "timeout": lambda: [AgentTurnTimeoutError(300.0)],
    "invalid-after-correction": lambda: [
        _plan(_workstream("ghost", continue_hypothesis=True)),
        _plan(_workstream("ghost", continue_hypothesis=True)),
    ],
}
_FAULT_ERRORS: dict[str, type[BaseException]] = {
    "crash": _PlannerCrashError,
    "timeout": AgentTurnTimeoutError,
    "invalid-after-correction": DynamicPlanningError,
}


@pytest.mark.parametrize("fault", sorted(_TURN_FAULTS))
@pytest.mark.parametrize("faulted_turns", [1, 2])
def test_planner_turn_faults_retry_with_the_workstream_bound(
    tmp_path: Path, fault: str, faulted_turns: int
) -> None:
    """Chaos seeds 1019, 3004, 3022, 3028: a planner fault ended the run at once.

    A planner turn fault is retried like an implementer attempt, up to
    max_retries_per_round turns in a row; only a spent bound ends the run.
    """
    replies = [reply for _ in range(faulted_turns) for reply in _TURN_FAULTS[fault]()]
    script = _Script(
        {
            ORCHESTRATOR.id: [*replies, _plan(_workstream("a"))],
            IMPLEMENTER.id: [{"summary": "No viable change.", "outcome": "disproven"}],
        }
    )
    options = _options(max_rounds=1, max_retries_per_round=2)

    if faulted_turns < options.max_retries_per_round:
        status, state = _execute(tmp_path, script, options)
        assert status is RunStatus.SUCCEEDED
        assert [item.hypothesis_id for item in state.workstreams] == ["a"]
        return
    with pytest.raises(_FAULT_ERRORS[fault]):
        _execute(tmp_path, script, options)
