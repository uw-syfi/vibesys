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
