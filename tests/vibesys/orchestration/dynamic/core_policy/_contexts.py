"""Generated prompt contexts of the dynamic search, and how production renders each one.

A context is the typed data the search sends the render operation. Its template is the file
of the role it asks (the role declares it), and the exchanges core starts (a correction of a
reply, a resumed turn) render core's own contexts through the role's declared exchange
templates. Every identifier a generator makes carries a distinctive prefix, so a test can ask
whether a fact reached the text without a word of the template matching by accident.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from hypothesis import strategies as st
from tests.vibesys.domain_sections import EMPTY_DOMAIN_SECTIONS
from tests.vibesys.orchestration.dynamic.prompt_variables import context_variables

import vibesys.orchestration.dynamic.core_policy.prompts as templates
from vibesys.metrics import MetricRow
from vibesys.orchestration.dynamic.core_policy.api import run_prompt_variables
from vibesys.orchestration.dynamic.search.api import (
    IMPLEMENTER,
    JUDGE,
    PLANNER,
    PROFILER,
    EvidenceCitation,
    ImplementPrompt,
    PlannerCorrection,
    PlannerPrompt,
    ProfilePrompt,
    PromptTemplate,
    ReviewEvaluation,
    ReviewPrompt,
    dynamic,
)
from vibesys.prompts import CORE_PROTOCOL_DIR, CoreProtocolTemplate
from vs_core.api import (
    EvidenceId,
    EvidenceKind,
    InvocationId,
    InvocationRef,
    ObservationStatus,
    RevisionId,
    RevisionRef,
    SessionId,
    WaitEnd,
)
from vs_core.api.search import (
    ReplyCorrection,
    ReplyError,
    ReplyErrorCode,
    ResumeFacts,
    WaitUnrecorded,
)
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import ProfileField

if TYPE_CHECKING:
    from pydantic import BaseModel

ROOT = Path(templates.__file__).parent
OBJECTIVE = "Raise queue throughput."
# Immutable once built; its compiled-template cache is shared by every example.
RENDERER = TemplateRenderer(ROOT, fallback_roots=(CORE_PROTOCOL_DIR,))

type Template = PromptTemplate | CoreProtocolTemplate

TEMPLATE_FILES: dict[Template, str] = {
    PromptTemplate.PORTFOLIO: PLANNER.prompt,
    PromptTemplate.PORTFOLIO_CORRECTION: PLANNER.prompt,
    PromptTemplate.IMPLEMENT: IMPLEMENTER.prompt,
    PromptTemplate.REVIEW: JUDGE.prompt,
    PromptTemplate.PROFILE_REQUEST: PROFILER.prompt,
    CoreProtocolTemplate.RESUME: IMPLEMENTER.exchanges.resume or "",
    CoreProtocolTemplate.REPLY_CORRECTION: IMPLEMENTER.exchanges.correction or "",
    CoreProtocolTemplate.WAIT_UNRECORDED: IMPLEMENTER.exchanges.wait_unrecorded or "",
}
"""The template file production renders for each kind of context."""

FRESH_TURNS = (
    PromptTemplate.PORTFOLIO,
    PromptTemplate.PORTFOLIO_CORRECTION,
    PromptTemplate.IMPLEMENT,
    PromptTemplate.REVIEW,
    PromptTemplate.PROFILE_REQUEST,
)
"""The templates that open a fresh agent turn."""


def revision(name: str) -> RevisionRef:
    return RevisionRef(revision_id=RevisionId(root=name), digest=f"git-commit:{name}")


def tokens(prefix: str) -> st.SearchStrategy[str]:
    """Text that no template wording contains: ``prefix`` and a few letters."""
    return st.from_regex(rf"{prefix}[a-f]{{4}}", fullmatch=True)


REVISIONS = st.sampled_from(("rev-a1", "rev-b2", "rev-c3")).map(revision)
TEXT = tokens("zq")
CITATIONS = st.lists(
    st.builds(
        EvidenceCitation, location=tokens("loc"), revision=st.none() | tokens("at"), purpose=TEXT
    ),
    max_size=2,
).map(tuple)
METRIC = st.builds(
    MetricRow,
    name=tokens("metric"),
    value=st.integers(0, 10**6).map(float),
    direction=st.sampled_from(("max", "min")),
    unit=st.none() | tokens("u"),
)
METRICS = st.lists(METRIC, max_size=2).map(tuple)


@st.composite
def _history(draw: st.DrawFn) -> list[dict[str, object]]:
    """Hypothesis rows and profile rows, each with its own prefix."""
    rows: list[dict[str, object]] = [
        {
            "hypothesis_id": f"hyp{index}",
            "sequence": index + 1,
            "title": f"title{index}",
            "status": draw(st.sampled_from(("running", "finished"))),
            "strategy": draw(st.sampled_from(("available", "parked"))),
            "summary": draw(st.just("") | TEXT),
            "candidate": draw(st.none() | REVISIONS),
            "metrics": draw(METRICS),
            "failure_tail": draw(st.just("") | TEXT),
        }
        for index in range(draw(st.integers(0, 3)))
    ]
    rows.extend(
        {
            "kind": "profile",
            "profile_id": f"prof{index}",
            "sequence": 10 + index,
            "revision": draw(REVISIONS),
            "question": f"question{index}",
            "decision_impact": f"impact{index}",
            "status": draw(st.sampled_from(("running", "observed", "failed"))),
            "diagnosis": draw(st.just("") | TEXT),
        }
        for index in range(draw(st.integers(0, 2)))
    )
    return rows


@st.composite
def _buildable(draw: st.DrawFn) -> list[dict[str, object]]:
    return [
        {
            "option_id": f"option{index}",
            "hypothesis_id": f"hyp{index}",
            "revision": draw(REVISIONS),
            "accuracy_evidence": EvidenceId(root=f"ev{index}"),
            "submission_index": index,
            "latest_verified": draw(st.booleans()),
            "best_partial": draw(st.booleans()),
            "metric": draw(st.none() | METRIC),
        }
        for index in range(draw(st.integers(0, 2)))
    ]


@st.composite
def planner_fields(draw: st.DrawFn, *, problem: object = None) -> dict[str, object]:
    """The fields a planning request and its correction share."""
    fields: tuple[str, ...] = tuple(
        draw(st.lists(st.sampled_from(ProfileField), unique=True)),
    )
    return {
        "capacity": draw(st.integers(0, 3)),
        "in_flight": draw(st.integers(0, 3)),
        "remaining": draw(st.integers(0, 9)),
        "base_revision": draw(REVISIONS),
        "base_accuracy_passed": draw(st.none() | st.booleans()),
        "offer_snapshot": draw(tokens("snap")),
        "buildable": draw(_buildable()),
        "history": draw(_history()),
        "older_ids": tuple(draw(st.lists(tokens("old"), max_size=2, unique=True))),
        "baseline": draw(METRICS),
        "input_failure": draw(
            st.none()
            | st.just({"kind": "benchmark-failed", "output": "bench-output"})
            | st.just({"kind": "readings-undecodable"})
        ),
        "profiling": draw(st.booleans()),
        "profile_fields": fields,
        "problem": problem,
    }


PLANNER_PROMPTS = planner_fields().map(
    lambda fields: PlannerPrompt.model_validate(fields, strict=False)
)

TURN = InvocationRef(
    session_id=SessionId(root="session"), invocation_id=InvocationId(root="turn"), generation=0
)
PROBLEMS = st.sampled_from(
    (
        {"kind": "no-reply"},
        {"kind": "no-reply", "ended": "timed-out"},
        {"kind": "underfilled", "scheduled": 1, "wanted": 2},
        {
            "kind": "unparseable",
            "errors": (ReplyError(path=("workstreams", 0, "task"), code=ReplyErrorCode.MISSING),),
        },
        {
            "kind": "violations",
            "violations": (
                {
                    "path": ("workstreams", 0, "hypothesis_id"),
                    "code": "hypothesis-id-used",
                    "subject": "hyp0",
                },
            ),
        },
    )
)
PLANNER_CORRECTIONS = st.builds(
    lambda fields, problem: PlannerCorrection.model_validate(
        {**fields, "of": TURN, "problem": problem}, strict=False
    ),
    planner_fields(),
    PROBLEMS,
)
BLOCKERS = st.builds(
    dict,
    kind=st.sampled_from(("failed", "rejected")),
    summary=TEXT,
    next_step=st.just("") | tokens("step"),
)
IMPLEMENT_PROMPTS = st.builds(
    lambda **fields: ImplementPrompt.model_validate(fields, strict=False),
    hypothesis_id=tokens("hid"),
    hypothesis=TEXT,
    task=tokens("task"),
    pass_criteria=tokens("crit"),
    parent_revision=REVISIONS,
    evidence=CITATIONS,
    worktree_revision=st.none() | REVISIONS,
    prior_revision=st.none() | REVISIONS,
    rejection=st.none() | tokens("reject"),
    prior_failure_tail=st.just("") | tokens("tail"),
    last_blocker=st.none() | BLOCKERS,
    turns_without_candidate=st.integers(0, 3),
)
EVALUATIONS = st.lists(
    st.builds(
        ReviewEvaluation,
        evidence_id=tokens("ev").map(lambda value: EvidenceId(root=value)),
        kind=st.sampled_from((EvidenceKind.CORRECTNESS, EvidenceKind.BENCHMARK)),
        revision=REVISIONS,
        status=st.just(ObservationStatus.SUCCEEDED),
        passed=st.none() | st.booleans(),
        metrics=METRICS,
        feedback=st.just("") | tokens("out"),
    ),
    max_size=2,
).map(tuple)
REVIEW_PROMPTS = st.builds(
    ReviewPrompt,
    hypothesis_id=tokens("hid"),
    hypothesis=TEXT,
    pass_criteria=tokens("crit"),
    candidate=REVISIONS,
    summary=tokens("sum"),
    evidence=CITATIONS,
    evaluations=EVALUATIONS,
)
PROFILE_PROMPTS = st.builds(
    ProfilePrompt,
    question=tokens("ask"),
    required_fields=st.lists(st.sampled_from(ProfileField), unique=True).map(
        lambda fields: tuple(field.value for field in fields)
    ),
    target=REVISIONS,
)
ROLES = ("implementer", "judge", "profiler")
CONTEXTS: dict[Template, st.SearchStrategy[BaseModel]] = {
    PromptTemplate.PORTFOLIO: PLANNER_PROMPTS,
    PromptTemplate.PORTFOLIO_CORRECTION: PLANNER_CORRECTIONS,
    PromptTemplate.IMPLEMENT: IMPLEMENT_PROMPTS,
    PromptTemplate.REVIEW: REVIEW_PROMPTS,
    PromptTemplate.PROFILE_REQUEST: PROFILE_PROMPTS,
    CoreProtocolTemplate.RESUME: st.one_of(
        st.builds(
            ResumeFacts,
            agent=st.sampled_from(ROLES),
            retained=st.none() | REVISIONS,
            ended_by=st.none(),
        ),
        st.builds(
            ResumeFacts,
            agent=st.sampled_from(ROLES),
            retained=st.none() | REVISIONS,
            ended_by=st.just(WaitEnd.QUEUE_BUDGET),
            queue_budget_spent=st.sampled_from([True]),
        ),
        st.builds(
            ResumeFacts,
            agent=st.sampled_from(ROLES),
            retained=st.none() | REVISIONS,
            ended_by=st.sampled_from((WaitEnd.LIMIT, WaitEnd.RUN_END)),
            queue_budget_spent=st.booleans(),
        ),
    ),
    CoreProtocolTemplate.REPLY_CORRECTION: st.builds(
        ReplyCorrection,
        role=st.sampled_from(("planner", *ROLES)),
        errors=st.lists(
            st.builds(
                ReplyError,
                path=st.lists(st.sampled_from(("reply", "summary", 0)), max_size=2).map(tuple),
                code=st.sampled_from(ReplyErrorCode),
            ),
            min_size=1,
            max_size=2,
        ).map(tuple),
    ),
    CoreProtocolTemplate.WAIT_UNRECORDED: st.builds(WaitUnrecorded, role=st.sampled_from(ROLES)),
}


def variables(
    context: BaseModel, *, agent_evaluation: bool = False, notes: str = ""
) -> dict[str, object]:
    """What the render owner passes the template: run-level variables, then the context's own."""
    return {
        **run_prompt_variables(
            domain_sections=EMPTY_DOMAIN_SECTIONS,
            objective=OBJECTIVE,
            environment_notes=notes,
            agent_evaluation=agent_evaluation,
        ),
        **context_variables(context),
    }


def render(template: Template, context: BaseModel, **options: bool | str) -> str:
    """The prompt production renders for ``context``, under strict undefined variables."""
    return RENDERER.render_template(TEMPLATE_FILES[template], **variables(context, **options))  # ty: ignore[invalid-argument-type]


def role_ids() -> tuple[str, ...]:
    """The ids of the roles the search declares."""
    return tuple(dynamic.ids.role_id(role.name).root for role in dynamic.roles)
