"""Slot scheduling, retry budgets, and winner selection across workstreams."""

from __future__ import annotations

import asyncio
import tempfile
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._support import (
    INPUT_BASELINE,
    EvaluationTransportError,
    JudgeTransportError,
    Script,
    baseline_run,
    dynamic_options,
    implementation,
    portfolio,
    requested_slots,
    throughput,
)

from vibesys.orchestration.dynamic import (
    PLUGIN,
    REGISTRATION,
    DynamicState,
)
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vs_runtime.api import (
    AgentCapability,
    BenchmarkEvaluation,
    MetricDirection,
    Run,
    RunFacts,
    RunStatus,
)
from vs_runtime.api.testing import FakeEvaluationGate, FakeRun

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_runtime.api import AgentRole, CandidateWorkspace, Workspace, Workspaces


def test_parallel_hypotheses_use_isolated_workspaces_and_adopt_best(tmp_path: Path) -> None:
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("prefill", "decode")],
            IMPLEMENTER.id: [implementation("prefill"), implementation("decode")],
            JUDGE.id: [
                {"passed": True, "analysis": "Candidate is correct."},
                {"passed": True, "analysis": "Candidate is correct."},
            ],
        }
    )

    async def scenario() -> tuple[RunStatus, FakeRun]:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(
                domain_id="generic",
                objective="Improve throughput.",
                accuracy_configured=True,
                benchmark_configured=True,
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
        run.evaluation.script_benchmark(
            INPUT_BASELINE,
            BenchmarkEvaluation(
                executed=True,
                metric_name="throughput",
                metric_value=10.0,
                metric_direction=MetricDirection.MAXIMIZE,
                row={"throughput": 10.0},
            ),
            BenchmarkEvaluation(
                executed=True,
                metric_name="throughput",
                metric_value=12.0,
                metric_direction=MetricDirection.MAXIMIZE,
                row={"throughput": 12.0},
            ),
        )
        status = await PLUGIN.orchestrate(run, dynamic_options())
        return status, run

    status, run = asyncio.run(scenario())

    assert status is RunStatus.SUCCEEDED
    assert len(run.workspaces.candidates) == 2
    assert all(candidate.discarded for candidate in run.workspaces.candidates)
    implementers = [session for session in run.agents.sessions if session.role.id == IMPLEMENTER.id]
    assert {session.member_id for session in implementers} == {"prefill", "decode"}
    assert len({session.workspace.id for session in implementers}) == 2
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    assert state.winner_revision == state.workstreams[1].candidate_revision
    assert state.adoption_pending is False
    assert run.workspaces.root.restore_calls[-1][0] == state.winner_revision
    assert [item.hypothesis_id for item in state.search.hypotheses] == ["prefill", "decode"]
    assert [item.round_number for item in state.search.rounds] == [1, 2]
    assert [item.perf_provenance for item in state.search.rounds] == [
        "framework",
        "framework",
    ]
    measurements = [item.measurement for item in state.search.hypotheses]
    assert all(item is not None for item in measurements)
    assert [item.value if item is not None else None for item in measurements] == [10.0, 12.0]


def test_nonparallel_runtime_fails_before_any_agent_turn(tmp_path: Path) -> None:
    """Without isolated candidates no workstream can start; fail before planning."""
    script = Script({})

    async def scenario() -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            responder=script.respond,
            supported_extra_tools={"evaluation", "profiler"},
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
            },
            supports_parallel_candidates=False,
        )
        with pytest.raises(RuntimeError, match="does not support parallel candidates"):
            await PLUGIN.orchestrate(run, dynamic_options())
        return run

    run = asyncio.run(scenario())
    assert not run.agents.sessions
    assert script.calls == []


@pytest.mark.parametrize("cancel_orchestrate", [False, True], ids=["exception", "cancellation"])
def test_parallel_evaluations_are_drained_before_candidate_discard(
    tmp_path: Path,
    *,
    cancel_orchestrate: bool,
) -> None:
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("candidate")],
            IMPLEMENTER.id: [implementation("candidate")],
            JUDGE.id: [{"passed": True, "analysis": "Candidate is reviewable."}],
        }
    )

    async def scenario() -> tuple[FakeEvaluationGate, FakeEvaluationGate, FakeRun]:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(
                domain_id="generic",
                objective="Improve throughput.",
                accuracy_configured=True,
                benchmark_configured=True,
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
        # The input baseline is measured alone, before any candidate exists.
        run.evaluation.script_root_benchmark(INPUT_BASELINE)
        run.evaluation.script_accuracy(EvaluationTransportError())
        accuracy = run.evaluation.gate("accuracy", 0)
        benchmark = run.evaluation.gate("benchmark", 1)
        orchestrating = asyncio.ensure_future(
            PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1))
        )
        await accuracy.entered.wait()
        await benchmark.entered.wait()
        if cancel_orchestrate:
            orchestrating.cancel()
            with pytest.raises(asyncio.CancelledError):
                await orchestrating
        else:
            accuracy.release()
            await orchestrating
        return accuracy, benchmark, run

    accuracy, benchmark, run = asyncio.run(scenario())

    canceled_while_live = {
        kind
        for kind, gate in (("accuracy", accuracy), ("benchmark", benchmark))
        if gate.cancelled_while_live
    }
    expected = {"accuracy", "benchmark"} if cancel_orchestrate else {"benchmark"}
    assert canceled_while_live == expected
    assert accuracy.entered.is_set()
    assert benchmark.entered.is_set()
    assert benchmark.finished
    assert all(candidate.discarded for candidate in run.workspaces.candidates)


def test_failed_parallel_slot_is_retried_without_canceling_its_sibling(tmp_path: Path) -> None:
    attempts = {"fragile": 0, "steady": 0}
    reviews = {"fragile": 0, "steady": 0}

    def respond(
        role: AgentRole,
        _history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        if role.id == ORCHESTRATOR.id:
            return portfolio("fragile", "steady")
        hypothesis_id = "fragile" if "fragile" in message else "steady"
        if role.id == IMPLEMENTER.id:
            attempts[hypothesis_id] += 1
            return implementation(hypothesis_id)
        reviews[hypothesis_id] += 1
        if hypothesis_id == "fragile" and reviews[hypothesis_id] == 1:
            raise JudgeTransportError
        return {"passed": True, "analysis": "Candidate is correct."}

    async def scenario() -> FakeRun:
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
                for value in (10.0, 11.0)
            ),
        )
        await PLUGIN.orchestrate(run, dynamic_options(max_retries_per_round=2))
        return run

    run = asyncio.run(scenario())
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    # The retained implementation survives the judge failure; only the review is retried.
    assert attempts == {"fragile": 1, "steady": 1}
    assert reviews == {"fragile": 2, "steady": 1}
    assert all(item.phase.value == "evaluated" for item in state.workstreams)
    assert any(
        "dynamic workstream fragile failed" in call.message for call in run.observations.calls
    )


def test_noise_aware_multi_axis_frontier_drives_dispositions_and_winner(
    tmp_path: Path,
) -> None:
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("fast", "lean", "dominated")],
            IMPLEMENTER.id: [
                implementation("fast"),
                implementation("lean"),
                implementation("dominated"),
            ],
            JUDGE.id: [
                {"passed": True, "analysis": "Candidate is correct."},
                {"passed": True, "analysis": "Candidate is correct."},
                {"passed": True, "analysis": "Candidate is correct."},
            ],
        }
    )
    rows = (
        {"throughput": 100.0, "latency": 10.0},
        {"throughput": 90.0, "latency": 8.0},
        {"throughput": 99.0, "latency": 11.0},
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
            },
        )
        run.evaluation.script_benchmark(
            INPUT_BASELINE,
            *(
                BenchmarkEvaluation(
                    executed=True,
                    metric_name="throughput",
                    metric_value=row["throughput"],
                    metric_direction=MetricDirection.MAXIMIZE,
                    row=row,
                )
                for row in rows
            ),
        )
        await PLUGIN.orchestrate(
            run,
            dynamic_options(
                max_in_flight=3,
                metric_space={
                    "relative_noise": 0.05,
                    "objectives": [
                        {"name": "throughput", "direction": "max"},
                        {"name": "latency", "direction": "min"},
                    ],
                },
            ),
        )
        return run

    run = asyncio.run(scenario())
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    dispositions = {
        record.hypothesis_id: record.candidate_disposition for record in state.search.rounds
    }
    assert dispositions == {
        "fast": "pareto_frontier",
        "lean": "pareto_frontier",
        "dominated": "discard",
    }
    winner = next(item for item in state.workstreams if item.hypothesis_id == "fast")
    assert state.winner_revision == winner.candidate_revision


def test_failed_slot_sequence_is_not_reused_by_a_later_winner(tmp_path: Path) -> None:
    """A slot that failed before recording a round keeps its sequence to itself.

    Otherwise a later workstream reuses the number, and the winner lookup by
    round number resolves to the failed slot's unreviewed revision.
    """

    planned: list[str] = []

    def respond(
        role: AgentRole,
        _history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        if role.id == ORCHESTRATOR.id:
            # The first call fills both slots; each freed slot is refilled.
            ids = (
                (("base", "broken"), ("winner",))[len(planned)]
                if len(planned) < 2
                else (f"spare{len(planned)}",)
            )
            planned.append(ids[0])
            return portfolio(*ids)
        hypothesis_id = message.split("`")[1]
        if role.id == IMPLEMENTER.id:
            if hypothesis_id.startswith("spare"):
                return {"summary": "No viable change.", "outcome": "disproven"}
            return implementation(hypothesis_id)
        if hypothesis_id == "broken":
            raise JudgeTransportError
        return {"passed": True, "analysis": "Candidate is correct."}

    async def scenario() -> FakeRun:
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
                for value in (10.0, 20.0)
            ),
        )
        await PLUGIN.orchestrate(run, dynamic_options(max_rounds=2))
        return run

    run = asyncio.run(scenario())
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    sequences = [item.sequence for item in state.workstreams]
    assert len(sequences) == len(set(sequences))
    winner = next(item for item in state.workstreams if item.hypothesis_id == "winner")
    assert state.winner_revision == winner.candidate_revision


_FATES = st.sampled_from(("raise", "reject", "pass"))


@settings(max_examples=30, deadline=None)
@given(epochs=st.lists(st.tuples(_FATES, _FATES), min_size=1, max_size=3))
def test_winner_is_always_the_workstream_of_the_winning_round(
    epochs: list[tuple[str, str]],
) -> None:
    """For any mix of failed, rejected, and evaluated slots, adoption is consistent.

    Sequences stay unique, and the adopted revision is the candidate of the
    workstream that recorded the best trusted round.
    """
    fates = {
        f"e{epoch}-{slot}": fate
        for epoch, pair in enumerate(epochs, start=1)
        for slot, fate in zip("ab", pair, strict=True)
    }
    unplanned = deque(fates)

    def respond(
        role: AgentRole,
        _history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        if role.id == ORCHESTRATOR.id:
            return portfolio(*(unplanned.popleft() for _ in range(requested_slots(message))))
        hypothesis_id = next(name for name in fates if f"`{name}`" in message)
        if role.id == IMPLEMENTER.id:
            return implementation(hypothesis_id)
        if fates[hypothesis_id] == "raise":
            raise JudgeTransportError
        passed = fates[hypothesis_id] == "pass"
        return {"passed": passed, "analysis": "Reviewed.", "feedback": "" if passed else "No."}

    async def scenario(root: Path) -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=root,
            facts=RunFacts(domain_id="generic", objective="Improve.", benchmark_configured=True),
            responder=respond,
            supported_extra_tools={"evaluation", "profiler"},
            supports_parallel_candidates=True,
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
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
                for value in (10.0 * (index + 1) for index in range(len(fates)))
            ),
        )
        await PLUGIN.orchestrate(run, dynamic_options(max_rounds=len(epochs)))
        return run

    with tempfile.TemporaryDirectory() as directory:
        run = asyncio.run(scenario(Path(directory)))
        state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    sequences = [item.sequence for item in state.workstreams]
    assert len(sequences) == len(set(sequences))
    trusted = [
        record
        for record in state.search.rounds
        if record.official_evaluation and record.perf_metric is not None
    ]
    if not trusted:
        assert state.winner_revision is None
        return
    best = max(trusted, key=lambda record: record.perf_metric or 0.0)
    winner = next(item for item in state.workstreams if item.hypothesis_id == best.hypothesis_id)
    assert state.winner_revision == winner.candidate_revision == best.commit


def test_recorded_hypothesis_lineage_matches_the_branched_revision(tmp_path: Path) -> None:
    """Each hypothesis records the revision its workstream branched from, not a sibling."""
    planned = 0

    def respond(
        role: AgentRole,
        _history: tuple[str, ...],
        _message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        nonlocal planned
        if role.id == ORCHESTRATOR.id:
            planned += 1
            return portfolio(f"e{planned}-a", f"e{planned}-b", request_evaluation=False)
        return {"summary": "No viable change.", "outcome": "disproven"}

    async def scenario() -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            responder=respond,
            supported_extra_tools={"evaluation", "profiler"},
            supports_parallel_candidates=True,
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
            },
        )
        await PLUGIN.orchestrate(run, dynamic_options(max_rounds=2, judge_every=100))
        return run

    run = asyncio.run(scenario())
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    hypotheses = {item.hypothesis_id: item for item in state.search.hypotheses}
    rounds = {record.hypothesis_id: record.round_number for record in state.search.rounds}
    assert len(hypotheses) == len(state.workstreams) == 4
    for workstream in state.workstreams:
        hypothesis = hypotheses[workstream.hypothesis_id]
        assert hypothesis.parent_commit == workstream.parent_revision
        assert hypothesis.parent_round is None
        assert rounds[workstream.hypothesis_id] == workstream.sequence


def test_projected_round_budget_covers_every_recorded_round(tmp_path: Path) -> None:
    """The run's advertised round budget matches the rounds a full run records.

    Each epoch records one round per workstream, so a budget of epochs alone
    would show progress past 100%.
    """
    planned = 0

    def respond(
        role: AgentRole,
        _history: tuple[str, ...],
        _message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        nonlocal planned
        if role.id == ORCHESTRATOR.id:
            planned += 1
            return portfolio(f"e{planned}-a", f"e{planned}-b", request_evaluation=False)
        return {"summary": "No viable change.", "outcome": "disproven"}

    options = dynamic_options(max_rounds=2, max_in_flight=2, judge_every=100)

    async def scenario() -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            responder=respond,
            supported_extra_tools={"evaluation", "profiler"},
            supports_parallel_candidates=True,
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
            },
        )
        await PLUGIN.orchestrate(run, options)
        return run

    run = asyncio.run(scenario())
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    assert REGISTRATION.project_max_rounds is not None
    budget = REGISTRATION.project_max_rounds(options)
    assert max(record.round_number for record in state.search.rounds) == budget


def test_new_hypotheses_build_on_the_best_trusted_candidate(tmp_path: Path) -> None:
    """A later epoch's fresh hypothesis starts from the best evaluated candidate.

    Branching every hypothesis from the original root would make the final
    winner contain at most one hypothesis's change.
    """
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("base"), portfolio("stacked")],
            IMPLEMENTER.id: [implementation("base"), implementation("stacked")],
            JUDGE.id: [
                {"passed": True, "analysis": "Candidate is correct."},
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
                for value in (10.0, 20.0)
            ),
        )
        await PLUGIN.orchestrate(run, dynamic_options(max_rounds=2, max_in_flight=1))
        return run

    run = asyncio.run(scenario())
    state = asyncio.run(run.state.load(DynamicState))
    assert state is not None
    base, stacked = state.workstreams
    assert base.candidate_revision is not None
    assert stacked.parent_revision == base.candidate_revision
    planner_messages = [message for role, _, message in script.calls if role == ORCHESTRATOR.id]
    assert f"`{base.candidate_revision}`" in planner_messages[1]
    assert state.winner_revision == stacked.candidate_revision


def test_repeated_stage_failures_are_bounded(tmp_path: Path) -> None:
    """A stage that always fails gives up after the retry budget and marks the slot failed."""
    judge_calls = 0
    planner_calls = 0
    run: FakeRun | None = None

    def respond(
        role: AgentRole,
        _history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        nonlocal judge_calls, planner_calls
        if role.id == ORCHESTRATOR.id:
            planner_calls += 1
            return portfolio("doomed" if planner_calls == 1 else "other")
        if role.id == IMPLEMENTER.id:
            if "`other`" in message:
                return {"summary": "No viable change.", "outcome": "disproven"}
            return implementation("doomed")
        judge_calls += 1
        if judge_calls == 3:
            # Stop at the next checkpoint: the refill after the slot gives up.
            assert run is not None
            run.control.fail_with(RuntimeError("stop"))
        raise JudgeTransportError

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
            },
        )
        options = dynamic_options(max_rounds=2, max_in_flight=1, max_retries_per_round=3)
        with pytest.raises(RuntimeError, match="stop"):
            await PLUGIN.orchestrate(run, options)
        run.control.fail_with(None)
        await PLUGIN.orchestrate(run, options)
        return run

    finished = asyncio.run(scenario())
    assert judge_calls == 3
    # Resume does not reimplement the slot that exhausted its stage retries.
    doomed = [
        session
        for session in finished.agents.sessions
        if session.role.id == IMPLEMENTER.id and "`doomed`" in session.history[0]
    ]
    assert len(doomed) == 1
    state = asyncio.run(finished.state.load(DynamicState))
    assert state is not None
    assert state.workstreams[0].phase.value == "failed"
    assert state.winner_revision is None


def test_freed_slot_is_refilled_while_a_slow_sibling_still_runs(tmp_path: Path) -> None:
    """A finished workstream frees its slot for new work before its sibling ends.

    The first candidate benchmark is held; the other workstream finishes, and
    the next planning call must fill that one free slot while the held
    workstream is still in flight.
    """
    headers: list[str] = []

    def respond(
        role: AgentRole,
        _history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        if role.id == ORCHESTRATOR.id:
            headers.append(message.split("\n", 1)[0])
            if len(headers) > 1:
                held_candidate.release()
            slots = requested_slots(message)
            return portfolio(*(f"h{len(headers)}-{slot}" for slot in range(slots)))
        if role.id == IMPLEMENTER.id:
            return implementation(next(n for n in message.split("`") if n.startswith("h")))
        return {"passed": True, "analysis": "Reviewed.", "feedback": ""}

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
        },
    )
    run.evaluation.default_benchmark = throughput(2.0)
    run.evaluation.script_benchmark(INPUT_BASELINE)
    # The first candidate benchmark is held until the next planning call, so a
    # runtime that never refills a freed slot never finishes.
    held_candidate = run.evaluation.gate("benchmark", 1)

    async def scenario() -> None:
        options = dynamic_options(max_rounds=2, official_eval_every=1)
        assert await PLUGIN.orchestrate(run, options) is RunStatus.SUCCEEDED

    asyncio.run(scenario())
    assert held_candidate.released
    assert held_candidate.finished
    assert headers[1].startswith("Schedule at most 1 new workstreams for free slots. 1 other")


class _WorkspaceCreationError(RuntimeError):
    """Synthetic transient `git worktree add` failure."""


class _FlakyWorkspaces:
    """Workspaces whose first candidate creations fail, then delegate."""

    def __init__(self, delegate: Workspaces, *, failures: int) -> None:
        self.delegate = delegate
        self.failures = failures

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
        if self.failures > 0:
            self.failures -= 1
            message = "git worktree add failed: index.lock exists"
            raise _WorkspaceCreationError(message)
        return await self.delegate.create_candidate(from_revision, member_id=member_id)

    async def adopt(self, revision: str) -> None:
        await self.delegate.adopt(revision)

    async def export_patch(self, revision: str) -> str:
        return await self.delegate.export_patch(revision)


def test_workspace_creation_error_spends_a_slot_retry_not_the_run(tmp_path: Path) -> None:
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("flaky")],
            IMPLEMENTER.id: [implementation("flaky")],
            JUDGE.id: [{"passed": True, "analysis": "Candidate is correct."}],
        }
    )

    async def scenario() -> tuple[RunStatus, DynamicState | None]:
        fake = baseline_run(tmp_path, script)
        fake.evaluation.script_benchmark(INPUT_BASELINE, throughput(10.0))
        run = Run(
            run_id=fake.run_id,
            facts=fake.facts,
            agents=fake.agents,
            workspaces=_FlakyWorkspaces(fake.workspaces, failures=1),
            evaluation=fake.evaluation,
            state=fake.state,
            control=fake.control,
            commands=fake.commands,
            skills=fake.skills,
            observations=fake.observations,
        )
        status = await PLUGIN.orchestrate(
            run, dynamic_options(max_rounds=1, max_in_flight=1, max_retries_per_round=3)
        )
        return status, await fake.state.load(DynamicState)

    status, state = asyncio.run(scenario())

    assert status is RunStatus.SUCCEEDED
    assert state is not None
    assert state.workstreams[0].phase.value == "evaluated"
    assert state.winner_revision == state.workstreams[0].candidate_revision


def test_continued_hypothesis_gets_its_own_retry_budget(tmp_path: Path) -> None:
    """Failures of an earlier workstream of a hypothesis do not count against its continuation."""
    script = Script(
        {
            ORCHESTRATOR.id: [
                portfolio("h"),
                portfolio("h", continue_hypothesis=True),
            ],
            IMPLEMENTER.id: [
                JudgeTransportError("implementer turn failed"),
                {
                    "summary": "Partial progress on h.",
                    "outcome": "continue",
                    "next_step": "Finish the kernel.",
                    "evidence": [],
                },
                JudgeTransportError("implementer turn failed"),
                implementation("h"),
            ],
            JUDGE.id: [{"passed": True, "analysis": "Candidate is correct."}],
        }
    )

    async def scenario() -> DynamicState | None:
        run = baseline_run(tmp_path, script)
        run.evaluation.script_benchmark(INPUT_BASELINE, throughput(10.0))
        status = await PLUGIN.orchestrate(
            run, dynamic_options(max_rounds=2, max_in_flight=1, max_retries_per_round=2)
        )
        assert status is RunStatus.SUCCEEDED
        return await run.state.load(DynamicState)

    state = asyncio.run(scenario())

    assert len([call for call in script.calls if call[0] == IMPLEMENTER.id]) == 4
    assert state is not None
    assert state.workstreams[0].phase.value == "evaluated"
    assert state.winner_revision == state.workstreams[0].candidate_revision
