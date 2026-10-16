"""Span labels: kinds declared at ``@search``, applied with ``ctx.label`` and ``label=``.

Generated workstream programs label their own spans (a later call replaces the fields it names),
link each workstream to the one before it, and label their agent turns as they are issued. The
same program runs live and with a save-and-restart after every step: both must hold the same
labels, and the labels must be the fold of the calls. Anything a search did not declare (a kind,
an attribute, a relation, a verdict) or a link to a span the run never recorded is refused with
a ``ContractError`` naming the key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.testing import FlowRun, Prompt, fake_search
from vs_sim.api.testing import examples

from .flow_support import (
    FACTS,
    ROLES,
    WORKER,
    Config,
    Plan,
    assert_trace_ended,
    assert_trace_well_formed,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine


class WorkAttrs(core.Value):
    """A workstream's attributes."""

    n: int


class TurnAttrs(core.Value):
    """An agent turn's attributes."""

    role: str


WORKSTREAM = core.SpanKind(
    "workstream", attrs=WorkAttrs, verdicts=("accepted", "rejected"), links=("builds_on",)
)
TURN = core.SpanKind("agent_turn", attrs=TurnAttrs)
KINDS = (WORKSTREAM, TURN)
ROOT = "flow:#1"

_ATTRS = st.integers(0, 9).flatmap(
    lambda n: st.sampled_from([WorkAttrs(n=n), {"n": n}])  # a model, or keys it validates
)
_OP = st.fixed_dictionaries(
    {},
    optional={
        "title": st.sampled_from(["a", "b", None]),
        "verdict": st.sampled_from(["accepted", "rejected", None]),
        "reason": st.sampled_from(["slower", "faster", None]),
        "attrs": _ATTRS,
    },
)
PROGRAMS = st.lists(st.lists(_OP, max_size=3), min_size=1, max_size=3)


def search_of(
    root: Callable[[core.Flow, Config], Coroutine[Any, Any, None]],
    spans: tuple[core.SpanKind, ...] = KINDS,
) -> core.SearchDef[Config]:
    return fake_search("flows", Config, ROLES, spans)(root)


async def workstream(
    ctx: core.Flow, ops: list[core.LabelFields], previous: core.SpanId | None
) -> None:
    links = () if previous is None else (core.SpanLink(rel="builds_on", to=previous),)
    ctx.label(WORKSTREAM, links=links)
    for number, op in enumerate(ops):
        turn = core.Label(TURN, title=f"turn {number}", attrs=TurnAttrs(role="worker"))
        session = ctx.agents.session(WORKER)
        await session.turn(Prompt(text="go"), within=10.0, label=turn)
        ctx.label(WORKSTREAM, **op)


def root_of(
    programs: list[list[core.LabelFields]],
) -> Callable[[core.Flow, Config], Coroutine[Any, Any, None]]:
    async def root(ctx: core.Flow, _config: Config) -> None:
        previous: core.SpanId | None = None
        async with ctx.group() as tasks:
            for number, ops in enumerate(programs):
                previous = tasks.start(f"w{number}", workstream, ops, previous).span
            async for _ in tasks:
                pass

    return root


def expected_label(ops: list[core.LabelFields], previous: core.SpanId | None) -> core.SpanLabel:
    """The fold of the calls: each names fields that replace the ones before."""
    links = () if previous is None else (core.SpanLink(rel="builds_on", to=previous),)
    label = core.SpanLabel(kind="workstream", links=links)
    for op in ops:
        update: dict[str, object] = {k: v for k, v in op.items() if k != "attrs"}
        if "attrs" in op:
            attrs = op["attrs"]
            model = attrs if isinstance(attrs, WorkAttrs) else WorkAttrs.model_validate(attrs)
            update["attrs_json"] = model.model_dump_json()
        label = label.model_copy(update=update)
    return label


def play(run: FlowRun[Config]) -> None:
    """Answer the turns in the order they were asked until the run finishes."""
    for _ in range(20):
        if run.finished is not None:
            return
        run.reply(run.expect_turn("go"), Plan(n=1))
        assert_trace_well_formed(run.trace(), run.view.now_at)


@settings(max_examples=examples(pr=4, nightly=60), deadline=None)
@given(programs=PROGRAMS)
def test_labels_are_the_fold_of_the_calls_and_replay_identically(
    programs: list[list[core.LabelFields]],
) -> None:
    runs = [
        FlowRun.start(search_of(root_of(programs)), Config(), restarting=restarting)
        for restarting in (False, True)
    ]
    for run in runs:
        play(run)
        assert run.finished is not None
        assert_trace_ended(run.trace())
    live, restarting = runs
    assert live.trace() == restarting.trace()
    previous: core.SpanId | None = None
    for number, ops in enumerate(programs):
        span = live.trace().span(f"flow:w{number}/#1")
        assert span is not None
        assert span.label == expected_label(ops, previous)
        assert span.status is core.SpanStatus.DONE  # a verdict never changes the status
        previous = span.id
        turns = [s for s in live.trace().spans if s.parent == span.id and s.name == "worker"]
        assert [s.label for s in turns] == [
            core.SpanLabel(kind="agent_turn", title=f"turn {n}", attrs_json='{"role":"worker"}')
            for n in range(len(ops))
        ]


def refused(label: Callable[[core.Flow], None], spans: tuple[core.SpanKind, ...] = KINDS) -> str:
    """The path of the ContractError a root that applies ``label`` first is refused with."""

    async def root(ctx: core.Flow, _config: Config) -> None:
        label(ctx)

    with pytest.raises(core.ContractError) as error:
        FlowRun.start(search_of(root, spans), Config())
    return ".".join(map(str, error.value.path))


@settings(max_examples=examples(pr=6, nightly=60), deadline=None)
@given(name=st.from_regex(r"[a-z]{1,8}", fullmatch=True))
def test_an_undeclared_kind_relation_verdict_or_attribute_is_refused_naming_it(name: str) -> None:
    ghost = core.SpanKind(name)
    assert refused(lambda ctx: ctx.label(ghost)) == "label.kind"
    if name not in WORKSTREAM.verdicts:
        assert (
            refused(lambda ctx: ctx.label(WORKSTREAM, verdict=name)) == "label.workstream.verdict"
        )
    if name not in WORKSTREAM.links:
        link = core.SpanLink(rel=name, to=ROOT)
        assert (
            refused(lambda ctx: ctx.label(WORKSTREAM, links=(link,)))
            == f"label.workstream.links.{name}"
        )
    if name != "n":
        assert (
            refused(lambda ctx: ctx.label(WORKSTREAM, attrs={"n": 1, name: 2}))
            == f"label.workstream.attrs.{name}"
        )


@settings(max_examples=examples(pr=6, nightly=60), deadline=None)
@given(key=st.from_regex(r"w[0-9]", fullmatch=True))
def test_a_link_to_a_span_the_run_never_recorded_is_refused(key: str) -> None:
    missing = f"flow:{key}/#1"
    link = core.SpanLink(rel="builds_on", to=missing)
    path = refused(lambda ctx: ctx.label(WORKSTREAM, links=(link,)))
    assert path == f"label.workstream.links.{missing}"


def test_a_span_keeps_the_kind_of_its_first_label() -> None:
    def twice(ctx: core.Flow) -> None:
        ctx.label(WORKSTREAM, title="a")
        ctx.label(TURN, title="b")

    assert refused(twice) == "label.kind"


def test_a_kind_is_declared_once_and_its_attributes_forbid_unknown_keys() -> None:
    class Loose(core.Value):
        model_config = core.Value.model_config | {"extra": "ignore"}

    with pytest.raises(core.ContractError, match="extra='forbid'"):
        core.SpanKind("loose", attrs=Loose)
    with pytest.raises(core.ContractError, match="distinct names"):
        search_of(root_of([]), (WORKSTREAM, core.SpanKind("workstream")))
    lookalike = core.SpanKind("workstream", attrs=WorkAttrs)
    assert refused(lambda ctx: ctx.label(lookalike)) == "label.kind"


@pytest.mark.parametrize("restarting", [False, True], ids=["live", "restarting"])
def test_a_turn_is_labelled_when_issued(*, restarting: bool) -> None:
    async def root(ctx: core.Flow, _config: Config) -> None:
        turn = core.Label(TURN, attrs={"role": "worker"})
        await ctx.agents.session(WORKER).turn(Prompt(text="go"), within=10.0, label=turn)

    run = FlowRun.start(search_of(root), Config(), restarting=restarting)
    turn = next(s for s in run.trace().spans if s.name == "worker")
    assert turn.label == core.SpanLabel(kind="agent_turn", attrs_json='{"role":"worker"}')
    root_span = run.trace().span(ROOT)
    assert root_span is not None
    assert root_span.label is None  # only what a flow labels carries a label
    play(run)
    assert run.finished is not None


@settings(max_examples=examples(pr=4, nightly=60), deadline=None)
@given(programs=PROGRAMS)
def test_folding_the_span_changes_of_every_step_gives_the_trace(
    programs: list[list[core.LabelFields]],
) -> None:
    run = FlowRun.start(search_of(root_of(programs)), Config())
    folded: dict[core.SpanId, core.Span] = {}
    before: core.RunTrace | None = None
    for _ in range(20):
        after = run.trace()
        for change, span in core.span_changes(before, after):
            assert (change is core.SpanChange.STARTED) == (span.id not in folded)
            assert (change is core.SpanChange.ENDED) <= span.ended
            folded[span.id] = span
        assert tuple(folded.values()) == after.spans
        if run.finished is not None:
            break
        before = after
        run.reply(run.expect_turn("go"), Plan(n=1))


@pytest.mark.parametrize("restarting", [False, True], ids=["live", "restarting"])
def test_a_flow_labels_the_spans_it_started_once_their_answers_are_in(*, restarting: bool) -> None:
    async def child(ctx: core.Flow) -> None:
        await ctx.agents.session(WORKER).turn(Prompt(text="go"), within=10.0)
        turn = ctx.operation_span
        assert turn is not None
        ctx.label(TURN, span=turn, attrs={"role": "worker"})

    async def root(ctx: core.Flow, _config: Config) -> None:
        async with ctx.group() as tasks:
            task = tasks.start("c", child)
            assert task.span is not None
            ctx.label(WORKSTREAM, span=task.span, title="started by the root")
            async for _ in tasks:
                pass

    run = FlowRun.start(search_of(root), Config(), restarting=restarting)
    play(run)
    labels = {s.id: s.label for s in run.trace().spans if s.label is not None}
    assert labels["flow:c/#1"] == core.SpanLabel(kind="workstream", title="started by the root")
    turn = next(s for s in run.trace().spans if s.name == "worker")
    assert labels[turn.id] == core.SpanLabel(kind="agent_turn", attrs_json='{"role":"worker"}')


def test_a_flow_cannot_label_a_span_it_did_not_start() -> None:
    assert refused(lambda ctx: ctx.label(TURN, span="flow:nowhere/#1")) == "label.span"

    async def child(ctx: core.Flow) -> None:
        ctx.label(WORKSTREAM, span=ROOT)

    async def root(ctx: core.Flow, _config: Config) -> None:
        async with ctx.group() as tasks:
            tasks.start("c", child)
            async for _ in tasks:
                pass

    with pytest.raises(core.ContractError, match="its own span or one it started"):
        FlowRun.start(search_of(root), Config())


# -- the span that adopts the winner ----------------------------------------------------------


ADOPTION = core.SpanKind(
    "adoption", attrs=WorkAttrs, verdicts=(core.ADOPTED, core.NOT_ADOPTED), links=("builds_on",)
)


def winner(name: str) -> core.Kept:
    revision = core.RevisionRef(revision_id=core.RevisionId(root=name), digest=f"{name}-digest")
    return core.Kept(
        settlement=core.Settlement(
            settlement_id=core.SettlementId(root=name),
            attempt=core.AttemptRef(attempt_id=core.AttemptId(root=name), generation=0),
            candidate=revision,
            assessments=(),
            eligible=True,
            retention="candidate",
            outcome="succeeded",
        )
    )


def adopting_run(
    chosen: list[core.Label | None], *, restarting: bool, adopting: bool = True
) -> FlowRun[Config]:
    """A run that names each label's winner in turn (the last stands), then waits for a timer."""

    async def root(ctx: core.Flow, _config: Config) -> None:
        for label in chosen:
            ctx.choose(winner("a"), label)
        await ctx.timer(5.0)

    definition = fake_search("flows", Config, ROLES, (*KINDS, ADOPTION))(root)
    run = FlowRun.start(definition, Config(), restarting=restarting, adopting=adopting)
    run.advance_to(5.0)
    return run


def adoptions(run: FlowRun[Config]) -> list[core.Span]:
    return [s for s in run.trace().spans if s.label is not None and s.label.kind == "adoption"]


@pytest.mark.parametrize("restarting", [False, True], ids=["live", "restarting"])
def test_the_span_that_adopts_the_winner_carries_the_chosen_label_and_is_adopted(
    *, restarting: bool
) -> None:
    label = core.Label(ADOPTION, title="a", attrs=WorkAttrs(n=3))
    run = adopting_run([label], restarting=restarting)
    (span,) = adoptions(run)
    assert span.label == core.SpanLabel(
        kind="adoption", title="a", attrs_json='{"n":3}', verdict=core.ADOPTED
    )
    assert span.type is core.SpanType.OPERATION
    assert run.finished is not None
    assert run.finished.result.outcome == "success"


@pytest.mark.parametrize("restarting", [False, True], ids=["live", "restarting"])
def test_the_adopting_span_is_labelled_before_core_answers_and_not_adopted_when_refused(
    *, restarting: bool
) -> None:
    label = core.Label(ADOPTION, attrs=WorkAttrs(n=1))
    run = adopting_run([label], restarting=restarting, adopting=False)
    proposal = run.expect(core.ProposeWinner)
    (waiting,) = adoptions(run)
    assert waiting.label is not None
    assert waiting.label.verdict is None
    assert not waiting.ended
    run.refuse_adoption(proposal, core.AdoptionFailureReason.CONTENT_MISMATCH)
    (span,) = adoptions(run)
    assert span.label is not None
    assert (span.label.verdict, span.label.reason) == (core.NOT_ADOPTED, "content-mismatch")


def test_a_choice_without_a_label_adopts_unlabelled_whatever_was_chosen_before() -> None:
    label = core.Label(ADOPTION, attrs=WorkAttrs(n=1))
    run = adopting_run([label, None], restarting=False)
    assert adoptions(run) == []


def test_a_label_whose_kind_lacks_the_adoption_verdicts_is_refused_where_it_is_given() -> None:
    bare = core.SpanKind("adoption", attrs=WorkAttrs, verdicts=(core.ADOPTED,))
    label = core.Label(bare, attrs=WorkAttrs(n=1))

    async def root(ctx: core.Flow, _config: Config) -> None:
        ctx.choose(winner("a"), label)

    definition = fake_search("flows", Config, ROLES, (*KINDS, bare))(root)
    with pytest.raises(core.ContractError) as error:
        FlowRun.start(definition, Config())
    assert ".".join(map(str, error.value.path)) == "choose.label.kind"
    undeclared = core.Label(core.SpanKind("ghost", verdicts=(core.ADOPTED, core.NOT_ADOPTED)))
    assert refused(lambda ctx: ctx.choose(winner("a"), undeclared)) == "label.kind"


def falling_back(
    steps: list[Callable[[core.Flow], None]], *, restarting: bool = False
) -> FlowRun[Config]:
    """A run that applies ``steps`` and waits for a timer, which ends it."""

    async def root(ctx: core.Flow, _config: Config) -> None:
        for step in steps:
            step(ctx)
        await ctx.timer(5.0)

    definition = fake_search("flows", Config, ROLES, (*KINDS, ADOPTION))(root)
    run = FlowRun.start(definition, Config(), restarting=restarting)
    run.advance_to(5.0)
    return run


@pytest.mark.parametrize("restarting", [False, True], ids=["live", "restarting"])
def test_falling_back_to_the_input_labels_the_adoption_of_a_run_that_chose_nothing_else(
    *, restarting: bool
) -> None:
    first = core.Label(ADOPTION, attrs=WorkAttrs(n=1))
    later = core.Label(ADOPTION, attrs=WorkAttrs(n=2))
    run = falling_back(
        [lambda ctx: ctx.fall_back_to_input(first), lambda ctx: ctx.fall_back_to_input(later)],
        restarting=restarting,
    )
    (span,) = adoptions(run)
    assert span.label == core.SpanLabel(
        kind="adoption", attrs_json='{"n":2}', verdict=core.ADOPTED
    )  # the later reading of the input replaced the first
    assert run.finished is not None
    assert run.finished.result.selection == core.TrustedBaseline(revision=FACTS.baseline)


def test_falling_back_to_the_input_keeps_a_candidate_already_chosen() -> None:
    chosen = core.Label(ADOPTION, attrs=WorkAttrs(n=1))
    input_label = core.Label(ADOPTION, attrs=WorkAttrs(n=2))
    run = falling_back(
        [
            lambda ctx: ctx.choose(winner("a"), chosen),
            lambda ctx: ctx.fall_back_to_input(input_label),
        ]
    )
    (span,) = adoptions(run)
    assert span.label is not None
    assert span.label.attrs_json == '{"n":1}'
    assert run.finished is not None
    assert isinstance(run.finished.result.selection, core.RetainedCandidate)
