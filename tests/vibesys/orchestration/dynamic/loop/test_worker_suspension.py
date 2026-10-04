"""Worker continuations composed with production sessions and executing Slurm."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from vibesys.api import RunStatus
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE
from vs_evaluation.api import EvidenceOutcome, TrustedEvidence
from vs_project.api import Project
from vs_slurm.fake_connector import active_jobs

from ._harness import (
    PASS,
    LoopInput,
    ScriptedAgents,
    Turn,
    edit_to,
    implemented,
    load_state,
    options,
    portfolio,
    run_loop,
    workstream,
)

if TYPE_CHECKING:
    from pathlib import Path


def _cited_input(tmp_path: Path) -> LoopInput:
    loop_input = LoopInput.create(tmp_path)
    skill = tmp_path / "resources" / "skills" / "objective-policy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: objective-policy\ndescription: Preserve accuracy.\n---\n# Objective policy\n",
        encoding="utf-8",
    )
    (skill / "floor.md").write_text("Preserve accuracy.\n", encoding="utf-8")
    (loop_input.root / "OBJECTIVE.md").write_text(
        "Raise queue throughput. Follow resources/skills/objective-policy/floor.md.\n",
        encoding="utf-8",
    )
    return replace(loop_input, skills_dirs=(skill,))


def _assert_settled(agent: Turn, handle: str, *kinds: str) -> None:
    assert handle in agent.prompt
    assert "Every evaluation you awaited is terminal" in agent.prompt
    evidence = [
        record
        for raw in agent.accepted_evidence(*kinds)
        if (record := TrustedEvidence.model_validate(raw)).evidence_id in agent.prompt
    ]
    assert {record.kind.value for record in evidence} == set(kinds)
    assert all(record.outcome is EvidenceOutcome.PASSED for record in evidence)


def _assert_evaluated_once(
    loop_input: LoopInput, run_id: str, identifier: str, metric: float
) -> None:
    projected = load_state(loop_input, run_id)
    member = next(item for item in projected.workstreams if item.hypothesis_id == identifier)
    assert member.phase is type(member.phase).EVALUATED
    assert member.budget.spent == 1
    assert member.budget.refunded == 0
    assert member.last_error is None
    assert member.review is not None
    assert member.review.passed
    assert member.evaluation is not None
    assert member.evaluation.accuracy_passed
    assert member.evaluation.benchmark_passed
    assert member.evaluation.metric_value == metric


def test_implementer_suspends_resumes_once_and_is_reviewed_and_adopted_with_a_healthy_peer(
    tmp_path: Path,
) -> None:
    loop_input = _cited_input(tmp_path)
    handles: dict[str, str] = {}

    def implement(agent: Turn) -> dict[str, object]:
        agent.set_value(4)
        (agent.workspace / "suspension-note.txt").write_text("retained work\n", encoding="utf-8")
        handles["implementer"] = agent.submit("accuracy", "benchmark")
        return {"kind": "waiting_for_evaluation", "handles": [handles["implementer"]]}

    def finish_implementation(agent: Turn) -> dict[str, object]:
        _assert_settled(agent, handles["implementer"], "accuracy", "benchmark")
        assert agent.value() == 4
        assert (agent.workspace / "suspension-note.txt").read_text(encoding="utf-8") == (
            "retained work\n"
        )
        return implemented("suspended")

    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("suspended"), workstream("healthy")))
        .implement("suspended", implement, finish_implementation)
        .judge("suspended", PASS)
        .implement("healthy", edit_to(3, "healthy"))
        .judge("healthy", PASS)
    )
    run = run_loop(loop_input, agents, options(max_in_flight=2))

    assert run.error is None, run.error
    assert run.succeeded is True
    assert run.status is RunStatus.COMPLETED
    assert run.notes() == []
    assert agents.unscripted == []
    assert active_jobs(loop_input.cluster) == ()
    state = load_state(loop_input, run.run_id)
    assert state.baseline is not None
    assert state.baseline.benchmark_passed
    assert state.baseline.metric_value == 1.0
    suspended, healthy = state.workstreams
    for member, metric in ((suspended, 4.0), (healthy, 3.0)):
        _assert_evaluated_once(loop_input, run.run_id, member.hypothesis_id, metric)
    assert state.winner_revision == suspended.candidate_revision
    assert not state.adoption_pending
    assert (loop_input.root / "queue.py").read_text(encoding="utf-8") == "VALUE = 4\n"
    continuations = tuple(state.lifecycle.continuations.values())
    assert {item.role for item in continuations} == {"implementer"}
    assert len(continuations) == 1
    assert {handle for continuation in continuations for handle in continuation.settlements} == set(
        handles.values()
    )
    resumes = [
        intent
        for intent in state.lifecycle.intents.values()
        if intent.kind is type(intent.kind).RESUME
    ]
    assert len(resumes) == 1
    assert all(intent.stage is type(intent.stage).COMPLETED for intent in resumes)
    first, resumed = agents.invocations(IMPLEMENTER.id, "suspended")
    assert first.session_key is not None
    assert resumed.session_key == first.session_key
    assert resumed.workspace == first.workspace
    assert first.invocation_id != resumed.invocation_id
    assert resumed.reuse_session
    assert len(agents.invocations(JUDGE.id, "suspended")) == 1
    assert len(agents.invocations(IMPLEMENTER.id, "healthy")) == 1
    assert len(agents.invocations(JUDGE.id, "healthy")) == 1
    runtime = Project.open(loop_input.root).state.portable_namespace(run.run_id, "runtime")
    assert (runtime.external_directory() / "effective-objective.md").read_text(
        encoding="utf-8"
    ) == ("Raise queue throughput. Follow .agents/skills/objective-policy/floor.md.\n")


class JudgeSuspensionUnavailableError(AssertionError):
    """The read-only Judge cannot obtain a handle its wait principal can own."""


@pytest.mark.xfail(
    strict=True,
    raises=JudgeSuspensionUnavailableError,
    reason=(
        "Judge suspension has no production path: Judge tool policy omits submission, "
        "while validate_evaluation_wait requires the submitting principal's own handle"
    ),
)
def test_judge_suspends_on_its_own_evaluation_after_implementer_resumes(tmp_path: Path) -> None:
    loop_input = _cited_input(tmp_path)
    handles: dict[str, str] = {}
    offered: list[tuple[str, ...]] = []

    def implement(agent: Turn) -> dict[str, object]:
        agent.set_value(4)
        handles["implementer"] = agent.submit("accuracy", "benchmark")
        return {"kind": "waiting_for_evaluation", "handles": [handles["implementer"]]}

    def finish_implementation(agent: Turn) -> dict[str, object]:
        _assert_settled(agent, handles["implementer"], "accuracy", "benchmark")
        assert agent.value() == 4
        return implemented("suspended")

    def judge(agent: Turn) -> dict[str, object]:
        offered.append(agent.tool_names())
        assert agent.value() == 4
        if "submit_evaluation" not in offered[-1]:
            return dict(PASS)
        handles["judge"] = agent.submit("benchmark")
        return {"kind": "waiting_for_evaluation", "handles": [handles["judge"]]}

    def finish_review(agent: Turn) -> dict[str, object]:
        _assert_settled(agent, handles["judge"], "benchmark")
        assert agent.value() == 4
        return dict(PASS)

    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("suspended")))
        .implement("suspended", implement, finish_implementation)
        .judge("suspended", judge, finish_review)
    )
    run = run_loop(loop_input, agents, options())
    assert run.error is None, run.error
    assert run.succeeded is True
    assert run.status is RunStatus.COMPLETED
    assert run.notes() == []
    assert agents.unscripted == []
    assert active_jobs(loop_input.cluster) == ()
    state = load_state(loop_input, run.run_id)
    assert state.baseline is not None
    assert state.baseline.benchmark_passed
    (member,) = state.workstreams
    _assert_evaluated_once(loop_input, run.run_id, member.hypothesis_id, 4.0)
    assert state.winner_revision == member.candidate_revision
    first, resumed = agents.invocations(IMPLEMENTER.id, "suspended")
    assert first.session_key == resumed.session_key
    assert len(offered) == 1
    assert "validate_evaluation_wait" in offered[0]
    if "submit_evaluation" not in offered[0]:
        message = "Judge cannot create an evaluation owned by its own wait principal"
        raise JudgeSuspensionUnavailableError(message)
    assert {item.role for item in state.lifecycle.continuations.values()} == {
        "implementer",
        "judge",
    }
    first_review, resumed_review = agents.invocations(JUDGE.id, "suspended")
    assert first_review.session_key == resumed_review.session_key
    resumes = [
        intent
        for intent in state.lifecycle.intents.values()
        if intent.kind is type(intent.kind).RESUME
    ]
    assert len(resumes) == 2
    assert all(intent.stage is type(intent.stage).COMPLETED for intent in resumes)
