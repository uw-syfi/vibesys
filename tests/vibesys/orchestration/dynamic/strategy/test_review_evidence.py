"""The judge is told every trusted evaluation of the candidate it reviews, or that none exists.

Each run goes through the production shell with scripted executors. A scripted implementer
measures its workspace through the evaluation tool during its turn, as the production
bridge does, and the test reads the typed context the strategy asked the renderer to render
for the judge. A judge that has to search the filesystem for results is the failure these
tests guard (LIVE-2: the judge said no evidence was provided after the implementer linked it).
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

from tests.vibesys.orchestration.dynamic.strategy._executors import Executors
from tests.vibesys.orchestration.dynamic.strategy._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.orchestration.dynamic.strategy._run import run_shell

import vibesys.orchestration.dynamic.core_policy.prompts as templates
from vibesys.orchestration.dynamic.strategy.api import (
    RenderRoleArtifacts,
    ReviewEvaluation,
    ReviewPrompt,
    dynamic_operation_registry,
)
from vs_core.api import EvidenceKind, ExecuteRegisteredOperation, RevisionRef
from vs_prompts.api import TemplateRenderer


def _executors(**changes: object) -> Executors:
    executors = Executors(
        planner=deque([plan_reply(implement("h1"))]),
        implementer=deque([implemented()]),
        # A rejected candidate is never measured again, so only the agent's own evaluation exists.
        judge=deque([reviewed(passed=False)]),
    )
    for name, value in changes.items():
        setattr(executors, name, value)
    return executors


def _review_prompts(executors: Executors) -> list[ReviewPrompt]:
    registry = dynamic_operation_registry()
    renders = [
        registry.decode(item.operation)
        for item in executors.seen
        if isinstance(item, ExecuteRegisteredOperation)
    ]
    return [
        item.context
        for item in renders
        if isinstance(item, RenderRoleArtifacts) and isinstance(item.context, ReviewPrompt)
    ]


def _text(prompt: ReviewPrompt, *, agent_evaluation: bool = True) -> str:
    variables = {name: getattr(prompt, name) for name in type(prompt).model_fields}
    variables.pop("template")
    root = Path(templates.__file__).parent
    return TemplateRenderer(root).render_template(
        "review.j2",
        **variables,
        objective="Raise throughput.",
        agent_evaluation=agent_evaluation,
        plan_example="",
    )


def test_the_judge_is_given_the_evidence_id_verdicts_and_metric_of_an_accepted_evaluation() -> None:
    executors = _executors(agent_evaluation=True, benchmark=lambda _commit: 123.0)
    run_shell(executors)

    (prompt,) = _review_prompts(executors)
    by_kind = {item.kind: item for item in prompt.evaluations}
    assert set(by_kind) == {EvidenceKind.CORRECTNESS, EvidenceKind.BENCHMARK}
    assert all(item.revision == prompt.candidate for item in prompt.evaluations)
    assert by_kind[EvidenceKind.CORRECTNESS].passed is True
    benchmark = by_kind[EvidenceKind.BENCHMARK]
    assert benchmark.passed is True
    assert [(row.name, row.value) for row in benchmark.metrics] == [("throughput", 123.0)]

    text = _text(prompt)
    for item in prompt.evaluations:
        assert item.evidence_id.root in text
    assert "throughput = 123.0" in text
    assert prompt.candidate.revision_id.root in text
    assert "PASS" in text


def test_a_failed_benchmark_reaches_the_judge_with_what_it_measured() -> None:
    executors = _executors(agent_evaluation=True, benchmark=lambda _commit: None)
    run_shell(executors)

    (prompt,) = _review_prompts(executors)
    benchmark = next(item for item in prompt.evaluations if item.kind is EvidenceKind.BENCHMARK)
    assert benchmark.passed is False
    assert benchmark.partial is not None
    assert benchmark.feedback == "benchmark failed"
    text = _text(prompt)
    assert "FAIL" in text
    assert "benchmark failed" in text


def test_the_judge_is_told_when_no_evaluation_exists() -> None:
    executors = _executors()
    run_shell(executors)

    (prompt,) = _review_prompts(executors)
    assert prompt.evaluations == ()
    assert "no trusted evaluation of this candidate exists yet" in _text(prompt)


def test_the_judge_may_not_fail_a_claim_it_has_no_measurement_for() -> None:
    """The rubric requires trusted evidence before a performance verdict (LIVE-2 finding 4)."""
    fields: dict[str, object] = {
        "candidate": _candidate(),
        "hypothesis_id": "h1",
        "hypothesis": "claim",
        "pass_criteria": "faster",
        "summary": "done",
    }
    prompt = ReviewPrompt.model_validate(fields)
    offered = _text(prompt, agent_evaluation=True)
    assert "fail the candidate on such a claim only when a listed result shows it" in " ".join(
        offered.split()
    )
    assert "measure the workspace once with `submit_evaluation`" in " ".join(offered.split())
    withheld = " ".join(_text(prompt, agent_evaluation=False).split())
    assert "submit_evaluation" not in withheld
    assert "do not fail the candidate for the missing measurement" in withheld


def _candidate() -> RevisionRef:
    return RevisionRef.of_git_commit("abc123")


def test_every_evaluation_of_the_candidate_is_listed_oldest_first() -> None:
    executors = _executors(agent_evaluation=True)
    run_shell(executors)
    (prompt,) = _review_prompts(executors)
    ids = [item.evidence_id.root for item in prompt.evaluations]
    assert ids == sorted(ids, key=lambda name: name.startswith("benchmark"))
    assert all(isinstance(item, ReviewEvaluation) for item in prompt.evaluations)
