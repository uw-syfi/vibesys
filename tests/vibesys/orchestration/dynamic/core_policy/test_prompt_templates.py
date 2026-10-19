"""Every prompt the dynamic search can request renders from the core policy's templates.

The search sends a typed prompt context; the render owner passes its fields, the reply fields
of the role's reply and the run-level variables to the template under strict undefined
variables. A template that reads a name no context supplies fails the render, which the search
sees as a failed render and ends the workstream, so each template is exercised over its
context space and checked for the facts of its context.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic.core_policy._contexts import (
    CONTEXTS,
    OBJECTIVE,
    PLANNER_PROMPTS,
    RENDERER,
    ROOT,
    TEMPLATE_FILES,
    render,
    tokens,
)

from vibesys.orchestration.dynamic.core_policy.api import planner_reply_example
from vibesys.orchestration.dynamic.models import ImplementPortfolioPlan, PortfolioPlan
from vibesys.orchestration.dynamic.search.api import (
    ImplementPrompt,
    PlannerCorrection,
    PlannerPrompt,
    ProfileHistoryRow,
    ProfilePrompt,
    PromptTemplate,
    ReviewPrompt,
)
from vibesys.prompts import CORE_PROTOCOL_DIR, PROMPTS_DIR, CoreProtocolTemplate, render_template
from vibesys.run.environment import agent_environment_notes
from vs_core.api import (
    MIN_SAMPLES_WITHOUT_FLOOR,
    AttemptId,
    Comparison,
    ComparisonVerdict,
    RevisionRef,
    Scope,
    WaitEnd,
)
from vs_core.api.search import ReplyCorrection, ResumeFacts, WaitUnrecorded
from vs_runtime.api.infrastructure import AgentPaths, RunEnvironmentView
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.prompts import CoreProtocolTemplate as CoreTemplate

ALL_TEMPLATES = [*PromptTemplate, *CoreProtocolTemplate]


def test_every_requestable_template_has_a_context_generator_and_a_template_file() -> None:
    assert set(CONTEXTS) == set(TEMPLATE_FILES) == set(ALL_TEMPLATES)
    for name in TEMPLATE_FILES.values():
        assert (ROOT / name).is_file() or (CORE_PROTOCOL_DIR / name).is_file(), name


@pytest.mark.parametrize("template", ALL_TEMPLATES, ids=lambda item: item.value)
@settings(max_examples=examples(pr=8, nightly=100))
@given(data=st.data())
def test_a_template_renders_for_every_context_of_its_kind(
    template: PromptTemplate | CoreTemplate, data: st.DataObject
) -> None:
    context = data.draw(CONTEXTS[template])

    text = render(template, context)

    assert text.strip()
    if template in (PromptTemplate.PORTFOLIO, PromptTemplate.IMPLEMENT, PromptTemplate.REVIEW):
        assert OBJECTIVE in text


@pytest.mark.parametrize("template", [PromptTemplate.IMPLEMENT, PromptTemplate.REVIEW])
@settings(max_examples=examples(pr=8, nightly=100))
@given(data=st.data(), offered=st.booleans())
def test_the_evaluation_tool_is_described_exactly_when_it_is_offered(
    template: PromptTemplate, data: st.DataObject, *, offered: bool
) -> None:
    context = data.draw(CONTEXTS[template])

    text = render(template, context, agent_evaluation=offered)

    assert ("submit_evaluation" in text) is offered
    assert ("validate_evaluation_wait" in text) is offered


@pytest.mark.regression
def test_the_evaluation_tool_says_when_the_base_is_measured_and_what_is_ranked() -> None:
    """The base is measured again on staleness and close calls, not once for the run.

    Before, the agent was told the base was measured "once for the run", while a close call
    without a declared noise measures it up to three times per staleness window. The tool's
    comparison also describes the runs held now, while a candidate is ranked by the value
    stored at its official measurement; the agent is told so.
    """
    text = RENDERER.render_template("agent_evaluation.j2", agent_evaluation=True)
    prose = " ".join(text.split())

    assert "once for the run" not in prose
    assert "again when the base's runs go stale or a comparison is too close to call" in prose
    assert "the value stored then is the one it is ranked by" in prose


# -- the planner ------------------------------------------------------------------------------


@settings(max_examples=examples(pr=8, nightly=100))
@given(context=PLANNER_PROMPTS)
def test_the_planner_prompt_states_the_run_it_plans_for(context: PlannerPrompt) -> None:
    text = render(PromptTemplate.PORTFOLIO, context)

    assert f"Schedule at most {context.capacity} new workstreams" in text
    assert context.base_revision.revision_id.root in text
    assert context.offer_snapshot in text
    for row in context.history:
        identity = row.profile_id if isinstance(row, ProfileHistoryRow) else row.hypothesis_id
        assert identity in text
    for option in context.buildable:
        assert f"option `{option.option_id}`" in text
        assert f"submission {option.submission_index}" in text
    for identifier in context.older_ids:
        assert identifier in text
    assert ("Older hypothesis IDs" in text) is bool(context.older_ids)
    assert ("Input baseline" in text) is bool(context.baseline)
    for row in context.baseline:
        assert row.name in text
    assert ("Input not measurable" in text) is (context.input_failure is not None)
    if context.input_failure is not None:
        assert (f"stage `{context.input_failure.stage}`" in text) is bool(
            context.input_failure.stage
        )
    assert ("choose a profile workstream" in text) is context.profiling
    assert ("none yet" in text) is not bool(context.history)
    assert "Correction required" not in text


@settings(max_examples=examples(pr=8, nightly=100))
@given(context=PLANNER_PROMPTS)
def test_the_planner_prompt_shows_a_reply_its_own_schema_accepts(context: PlannerPrompt) -> None:
    # live-1: Haiku put the plan inside a `findings` field, then left a workstream without
    # its fields; each failed schema check cost a 30 to 40 s planner turn.
    example = planner_reply_example()

    assert example in render(PromptTemplate.PORTFOLIO, context)
    for reply_type in (PortfolioPlan, ImplementPortfolioPlan):
        reply_type.model_validate_json(example)


@settings(max_examples=examples(pr=8, nightly=100))
@given(context=PLANNER_PROMPTS)
def test_a_workstream_of_the_reply_is_never_offered_a_sibling_as_parent(
    context: PlannerPrompt,
) -> None:
    # live-1: the planner named its own h1, not yet built, as the parent of h2.
    text = render(PromptTemplate.PORTFOLIO, context)

    assert "never another workstream of this reply" in text
    assert ("leave `parent_hypothesis_id` null" in text) is not bool(context.buildable)


@settings(max_examples=examples(pr=8, nightly=100))
@given(context=CONTEXTS[PromptTemplate.PORTFOLIO_CORRECTION])
def test_a_planner_correction_repeats_the_request_and_says_what_was_wrong(
    context: BaseModel,
) -> None:
    assert isinstance(context, PlannerCorrection)
    text = render(PromptTemplate.PORTFOLIO_CORRECTION, context)

    assert f"Schedule at most {context.capacity} new workstreams" in text
    assert planner_reply_example() in text
    assert text.count("Correction required: ") == 1
    assert context.problem is not None
    match context.problem.kind.value:
        case "underfilled":
            assert f"schedules {context.problem.scheduled} of {context.capacity} free slots" in text
        case "unparseable":
            assert "the reply violated the plan schema" in text
            for error in context.problem.errors:
                assert error.code.value in text
        case "violations":
            for violation in context.problem.violations:
                assert violation.subject in text
        case _:
            assert "planning turn" in text


# -- the implementer --------------------------------------------------------------------------


@settings(max_examples=examples(pr=8, nightly=100))
@given(context=CONTEXTS[PromptTemplate.IMPLEMENT])
def test_the_implementer_is_given_its_hypothesis_and_what_the_last_turn_left(
    context: BaseModel,
) -> None:
    assert isinstance(context, ImplementPrompt)
    text = render(PromptTemplate.IMPLEMENT, context)

    for fact in (context.hypothesis_id, context.hypothesis, context.task, context.pass_criteria):
        assert fact in text
    assert context.parent_revision.revision_id.root in text
    for citation in context.evidence:
        assert citation.location in text
    assert ("This continues your earlier work" in text) is (context.worktree_revision is not None)
    assert ("Feedback from the prior attempt" in text) is (context.rejection is not None)
    if context.rejection is not None:
        assert context.rejection in text
    assert ("The trusted check of your earlier attempt failed" in text) is bool(
        context.prior_failure_tail
    )
    assert ("ended without a candidate" in text) is (context.last_blocker is not None)
    assert ("still queued or running" in text) is context.baseline_pending
    assert ("trusted measurement failed" in text) is (context.input_failure is not None)
    if context.input_failure is not None and context.input_failure.output:
        assert context.input_failure.output in text
    if context.input_failure is not None and context.input_failure.stage:
        assert f"stage `{context.input_failure.stage}`" in text


@settings(max_examples=examples(pr=8, nightly=100))
@given(data=st.data(), offered=st.booleans())
def test_the_implementer_is_told_to_submit_early_and_not_to_repeat_a_passed_check(
    data: st.DataObject, *, offered: bool
) -> None:
    # live-1: implementer turns ran the local check 11 to 14 times and took 12 to 13 minutes.
    context = data.draw(CONTEXTS[PromptTemplate.IMPLEMENT])

    text = render(PromptTemplate.IMPLEMENT, context, agent_evaluation=offered)

    assert "Once the candidate builds and one local check passes, stop checking" in text
    assert "Never\nrerun a local check on files that already passed it" in text
    assert ("submit it for trusted evaluation" in text) is offered
    assert ("nominate it" in text.split("stop checking and")[1][:60]) is not offered


@settings(max_examples=examples(pr=8, nightly=100))
@given(data=st.data(), offered=st.booleans())
def test_the_implementer_keeps_working_while_its_evaluation_is_queued(
    data: st.DataObject, *, offered: bool
) -> None:
    # live run: implementers ended turns with `waiting_for_evaluation` just to wait, parking every
    # workstream, or busy-waited on queued GPU jobs with 10-minute `sleep` loops.
    context = data.draw(CONTEXTS[PromptTemplate.IMPLEMENT])

    text = " ".join(render(PromptTemplate.IMPLEMENT, context, agent_evaluation=offered).split())

    guidance = (
        "do not end the turn only to wait: keep implementing the remaining work",
        "validate it with tests that need no GPU",
        "Do not poll with `sleep` loops",
        "End the turn with `waiting_for_evaluation` only when nothing useful remains to implement",
    )
    assert all(line in text for line in guidance) is offered


@settings(max_examples=examples(pr=8, nightly=100))
@given(data=st.data(), summary=tokens("blk"), step=st.just("") | tokens("nxt"))
def test_a_retried_implementer_is_shown_its_own_blocker_and_narrowed_step(
    data: st.DataObject, summary: str, step: str
) -> None:
    # live-2: three turns in a row ended "not ready for trusted evaluation" on one workstream;
    # the retry received only the previous summary and no narrower step.
    context = data.draw(CONTEXTS[PromptTemplate.IMPLEMENT])
    assert isinstance(context, ImplementPrompt)
    context = ImplementPrompt.model_validate(
        {
            **context.model_dump(),
            "last_blocker": {"kind": "failed", "summary": summary, "next_step": step},
            "turns_without_candidate": 1,
        },
        strict=False,
    )

    text = render(PromptTemplate.IMPLEMENT, context)

    assert "The blocker you stated:" in text
    assert summary in text
    assert ("The narrower step you named for this turn:" in text) is bool(step)
    assert (step in text) if step else ("The narrower step you named" not in text)


@settings(max_examples=examples(pr=8, nightly=100))
@given(data=st.data())
def test_a_first_attempt_is_not_shown_a_blocker(data: st.DataObject) -> None:
    context = data.draw(CONTEXTS[PromptTemplate.IMPLEMENT])
    assert isinstance(context, ImplementPrompt)
    first = context.model_copy(update={"last_blocker": None, "turns_without_candidate": 0})

    assert "The blocker you stated:" not in render(PromptTemplate.IMPLEMENT, first)


@settings(max_examples=examples(pr=8, nightly=100))
@given(data=st.data())
def test_planner_and_implementer_share_one_first_measurable_step(data: st.DataObject) -> None:
    # live-2: the planner chose architecture-scale hypotheses that no single turn could finish.
    planner = data.draw(PLANNER_PROMPTS)
    implement = data.draw(CONTEXTS[PromptTemplate.IMPLEMENT])

    assert "first measurable step" in render(PromptTemplate.PORTFOLIO, planner)
    assert "first measurable step" in render(PromptTemplate.IMPLEMENT, implement)


# -- the judge and the profiler ------------------------------------------------------------------


@settings(max_examples=examples(pr=8, nightly=100))
@given(context=CONTEXTS[PromptTemplate.REVIEW])
def test_the_judge_is_given_the_candidate_and_every_trusted_evaluation_of_it(
    context: BaseModel,
) -> None:
    assert isinstance(context, ReviewPrompt)
    text = render(PromptTemplate.REVIEW, context)

    for fact in (context.hypothesis_id, context.pass_criteria, context.summary):
        assert fact in text
    assert context.candidate.revision_id.root in text
    assert ("no trusted evaluation of this candidate exists yet" in text) is not bool(
        context.evaluations
    )
    for item in context.evaluations:
        assert f"evidence `{item.evidence_id.root}`" in text
        assert item.feedback in text
        for row in item.metrics:
            assert row.name in text
    assert text.count("PASS") == sum(item.passed is True for item in context.evaluations)
    assert text.count("FAIL") == sum(item.passed is False for item in context.evaluations)
    assert text.count("result not decoded") == sum(
        item.passed is None for item in context.evaluations
    )


@settings(max_examples=examples(pr=8, nightly=100))
@given(data=st.data(), offered=st.booleans())
def test_the_judge_reads_evidence_and_the_diff_before_running_anything(
    data: st.DataObject, *, offered: bool
) -> None:
    # live-1: judges ran the local check 10 to 19 times in turns of 96 to 166 s.
    context = data.draw(CONTEXTS[PromptTemplate.REVIEW])

    text = render(PromptTemplate.REVIEW, context, agent_evaluation=offered)

    assert "Start from the referenced evidence and the candidate's diff" in text
    assert "do not run local checks, benchmarks" in text


@settings(max_examples=examples(pr=8, nightly=100))
@given(context=CONTEXTS[PromptTemplate.PROFILE_REQUEST])
def test_the_profiler_is_asked_the_question_of_the_revision_with_the_fields_it_must_answer(
    context: BaseModel,
) -> None:
    assert isinstance(context, ProfilePrompt)
    text = render(PromptTemplate.PROFILE_REQUEST, context)

    assert context.question in text
    assert context.target.revision_id.root in text
    assert OBJECTIVE in text
    assert ("Required measurement fields" in text) is bool(context.required_fields)
    for name in context.required_fields:
        assert name in text


# -- the exchanges core starts ------------------------------------------------------------------


@settings(max_examples=examples(pr=8, nightly=100))
@given(context=CONTEXTS[CoreProtocolTemplate.RESUME])
def test_a_resumed_turn_says_whether_its_wait_timed_out_and_what_was_retained(
    context: BaseModel,
) -> None:
    assert isinstance(context, ResumeFacts)
    text = render(CoreProtocolTemplate.RESUME, context)

    assert f"suspended {context.agent} turn" in text
    deadline = context.ended_by in {WaitEnd.LIMIT, WaitEnd.RUN_END}
    assert ("The suspension deadline expired" in text) is deadline
    assert ("Evaluations may still be running" in text) is deadline
    assert ("Every evaluation you awaited is terminal" in text) is (context.ended_by is None)
    assert ("Every other evaluation you awaited is terminal" in text) is (
        context.ended_by is WaitEnd.QUEUE_BUDGET
    )
    # A job core cancelled for its queue budget never ran, and the agent is told so.
    cancelled = "it never ran, so this says nothing about your candidate"
    assert (cancelled in text) is context.queue_budget_spent
    assert ("The retained workspace revision" in text) is (context.retained is not None)
    if context.retained is not None:
        assert context.retained.revision_id.root in text


_COMPARISONS = st.builds(
    Comparison,
    scope=st.just(Scope(owner=AttemptId(root="attempt"), generation=0)),
    metric=st.sampled_from(["throughput", "latency_ms"]),
    unit=st.sampled_from([None, "tokens/s", "ms"]),
    direction=st.sampled_from(["max", "min"]),
    candidate=st.text("0123456789abcdef", min_size=7, max_size=12).map(RevisionRef.of_git_commit),
    candidate_input=st.sampled_from(["sha256:one", "sha256:two", "sha256:three"]),
    base=st.text("0123456789abcdef", min_size=7, max_size=12).map(RevisionRef.of_git_commit),
    base_input=st.just("sha256:base"),
    candidate_value=st.floats(0.5, 1e4, allow_nan=False).map(lambda x: round(x, 3)),
    base_value=st.floats(0.5, 1e4, allow_nan=False).map(lambda x: round(x, 3)),
    ratio=st.floats(0.1, 10.0, allow_nan=False).map(lambda x: round(x, 4)),
    noise=st.floats(0.0, 1.0, allow_nan=False).map(lambda x: round(x, 4)),
    verdict=st.sampled_from(ComparisonVerdict),
    candidate_samples=st.integers(1, 6),
    base_samples=st.integers(1, 6),
)


_PINNED_COMPARISON = Comparison(
    scope=Scope(owner=AttemptId(root="attempt"), generation=0),
    metric="throughput",
    unit="tokens/s",
    direction="max",
    candidate=RevisionRef.of_git_commit("abcdef0"),
    candidate_input="sha256:candidate",
    base=RevisionRef.of_git_commit("0fedcba"),
    base_input="sha256:base",
    candidate_value=100.3,
    base_value=100.0,
    ratio=1.003,
    noise=0.0,
    verdict=ComparisonVerdict.WITHIN_NOISE,
    candidate_samples=1,
    base_samples=1,
)


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    context=CONTEXTS[CoreProtocolTemplate.RESUME],
    comparisons=st.lists(_COMPARISONS, max_size=3, unique_by=lambda c: c.candidate_input),
    settling=st.sets(st.sampled_from(["sha256:one", "sha256:two", "sha256:three"])),
    noise_declared=st.booleans(),
)
@example(
    context=ResumeFacts(agent="implementer", retained=None, ended_by=None),
    comparisons=[_PINNED_COMPARISON],
    settling=set(),
    noise_declared=False,
)
@example(
    context=ResumeFacts(agent="implementer", retained=None, ended_by=None),
    comparisons=[_PINNED_COMPARISON],
    settling={_PINNED_COMPARISON.candidate_input},
    noise_declared=True,
)
def test_a_resumed_turn_states_each_comparison_the_framework_made_and_none_it_did_not(
    context: BaseModel, comparisons: list[Comparison], settling: set[str], *, noise_declared: bool
) -> None:
    """Each comparison, and the rule its verdict was reached by, as core applies it.

    Before, the resume said a verdict meant "two noises" for every task, although a task that
    declares no per-run noise gets no verdict before ``MIN_SAMPLES_WITHOUT_FLOOR`` runs a side
    and is then judged by Welch's t test. The rule now comes from the run's comparison policy.
    A comparison core still runs is marked provisional, and the agent is told a repeat call
    reads the comparison as held then (before, it was told only not to repeat the call, so it
    never learned where a provisional comparison ended).
    """
    assert isinstance(context, ResumeFacts)
    pending = tuple(c.candidate_input for c in comparisons if c.candidate_input in settling)
    facts = ResumeFacts.model_validate(
        context.model_dump() | {"comparisons": tuple(comparisons), "settling": pending}
    )
    text = render(CoreProtocolTemplate.RESUME, facts, noise_declared=noise_declared)

    assert ("compared each measured candidate with its base" in text) is bool(comparisons)
    prose = " ".join(text.split())
    if comparisons:
        assert ("exceeds 2 noises" in prose) is noise_declared
        assert (f"at least {MIN_SAMPLES_WITHOUT_FLOOR} runs" in prose) is not noise_declared
        assert "two noises" not in prose
        assert "the value stored then is the one it is ranked by" in prose
        assert "returns the evaluation already made with its comparison as held now" in prose
        assert "Do not duplicate" not in prose
    assert prose.count("(provisional:") == len(pending)
    for item in comparisons:
        assert f"**{item.verdict.value.replace('_', ' ')}**" in text
        assert item.candidate.revision_id.root[:7] in text
        assert item.base.revision_id.root[:7] in text
        assert f"{item.ratio:.4f}" in text
        assert f"{item.noise:.4f}" in text
        assert f"{'higher' if item.direction == 'max' else 'lower'} is better" in text


@settings(max_examples=examples(pr=8, nightly=100))
@given(context=CONTEXTS[CoreProtocolTemplate.REPLY_CORRECTION])
def test_a_reply_correction_names_the_role_and_every_problem(context: BaseModel) -> None:
    assert isinstance(context, ReplyCorrection)
    text = render(CoreProtocolTemplate.REPLY_CORRECTION, context)

    assert f"Your previous {context.role} reply violated its output schema" in text
    assert text.count("\n- ") == len(context.errors)
    for error in context.errors:
        assert f"({error.code.value})" in text


@settings(max_examples=examples(pr=8, nightly=100))
@given(context=CONTEXTS[CoreProtocolTemplate.WAIT_UNRECORDED])
def test_an_unrecorded_wait_names_the_role_that_asked(context: BaseModel) -> None:
    assert isinstance(context, WaitUnrecorded)

    assert f"Your previous {context.role} reply" in render(
        CoreProtocolTemplate.WAIT_UNRECORDED, context
    )


def test_search_prompts_name_no_domain_command() -> None:
    # Domain commands live in the bundle's objective; the search's prompts stay generic.
    for template in sorted(ROOT.glob("*.j2")):
        assert "cpu_check" not in template.read_text(), template.name


# -- an agent whose Git metadata is read-only ----------------------------------------------------

_COMMIT_ADVICE = re.compile(
    r"\b(git commit|(commit|committing) (your|the|it|and|then|any|changes?)\b)", re.IGNORECASE
)
_READ_ONLY_GIT_NOTE = (
    "`.git` is read-only; submit_evaluation snapshots your working tree, including new files; "
    "do not commit"
)


def _docker_notes(*, git_writable: bool) -> str:
    """The notes every agent of a Docker run reads, rendered as the product renders them."""
    docker = render_template(
        "docker/prompt_notes.j2", template_dir=PROMPTS_DIR / "environments", history_root=None
    )
    view = RunEnvironmentView(
        paths=AgentPaths(), prompt_notes=str(docker), workspace_git_writable=git_writable
    )
    return agent_environment_notes(view)


def test_the_docker_notes_say_not_to_commit_exactly_when_git_is_read_only() -> None:
    assert _READ_ONLY_GIT_NOTE in _docker_notes(git_writable=False)
    assert "commit" not in _docker_notes(git_writable=True)


@pytest.mark.parametrize("template", [PromptTemplate.PORTFOLIO, PromptTemplate.IMPLEMENT])
@settings(max_examples=examples(pr=8, nightly=100))
@given(data=st.data(), offered=st.booleans())
def test_a_prompt_for_a_read_only_git_run_never_tells_an_agent_to_commit(
    template: PromptTemplate, data: st.DataObject, *, offered: bool
) -> None:
    # live run: the planner told implementers "(d) Commit and submit." in a run whose `.git`
    # the container mounts read-only, so each implementer burned a turn on a commit that failed.
    context = data.draw(CONTEXTS[template])

    text = render(
        template,
        context,
        agent_evaluation=offered,
        agent_git_writable=False,
        notes=_docker_notes(git_writable=False),
    )

    assert _READ_ONLY_GIT_NOTE in text
    assert _COMMIT_ADVICE.search(text) is None


@settings(max_examples=examples(pr=8, nightly=100))
@given(context=PLANNER_PROMPTS, writable=st.booleans())
def test_the_planner_is_told_implementers_cannot_commit_exactly_when_git_is_read_only(
    context: PlannerPrompt, *, writable: bool
) -> None:
    text = " ".join(render(PromptTemplate.PORTFOLIO, context, agent_git_writable=writable).split())

    assert ("never tell an implementer to commit" in text) is not writable
