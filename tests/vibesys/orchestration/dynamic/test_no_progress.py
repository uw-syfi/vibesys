"""A workstream whose implementer ends without a candidate is asked again only with news.

A turn that ends without a candidate reaching measurement costs agent time and tokens and
measures nothing. The first such turn earns one more (when the bound allows); later turns need
something the framework itself observed (a retained revision no earlier turn had, or a failure
output not seen before), never the agent's wording, and `max_unmeasured_turns` is a hard bound.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._values import config, revision

from vibesys.orchestration.dynamic.search.api import Blocker, BlockerKind, FailureKind, refusal
from vs_sim.api.testing import examples

PARENT = revision("parent")
BLOCKERS = st.builds(
    Blocker,
    kind=st.sampled_from(BlockerKind),
    summary=st.text(max_size=8),
    next_step=st.text(max_size=8),
    revision=st.none() | st.sampled_from([PARENT, revision("r1"), revision("r2")]),
    digest=st.none() | st.sampled_from(["d1", "d2", ""]),
)
BOUNDS = st.integers(1, 6)


def asked_until_refused(turns: list[Blocker], bound: int) -> tuple[int, FailureKind | None]:
    """Feed the turns one by one; how many were taken and why asking stopped (None: ran out)."""
    rules = config(max_unmeasured_turns=bound)
    taken: list[Blocker] = []
    for turn in turns:
        taken.append(turn)
        stop = refusal(tuple(taken), rules, PARENT)
        if stop is not None:
            return len(taken), stop.kind
    return len(taken), None


@settings(max_examples=examples(pr=8, nightly=100))
@given(turns=st.lists(BLOCKERS, min_size=1, max_size=12), bound=BOUNDS)
def test_a_workstream_takes_at_most_the_bound_in_turns_and_at_least_two_when_it_may(
    turns: list[Blocker], bound: int
) -> None:
    taken, why = asked_until_refused(turns, bound)
    assert taken <= bound
    if bound >= 2 and len(turns) >= 2:
        assert taken >= 2  # the first failure always earns one more
    if taken == bound:
        assert why is FailureKind.TURN_LIMIT
    else:
        assert why in {None, FailureKind.REPEATED_TURN}


@settings(max_examples=examples(pr=8, nightly=100))
@given(history=st.lists(BLOCKERS, min_size=1, max_size=8), bound=BOUNDS)
def test_a_refusal_names_the_turn_that_ended_and_the_bound_it_reached(
    history: list[Blocker], bound: int
) -> None:
    stop = refusal(tuple(history), config(max_unmeasured_turns=bound), PARENT)
    if stop is None:
        assert len(history) < bound
    elif stop.kind is FailureKind.TURN_LIMIT:
        assert (stop.turns, stop.limit) == (len(history), bound)
        assert stop.blocker == history[-1]
    else:
        assert stop.blocker == history[-1]
        assert len(history) > 1


@settings(max_examples=examples(pr=8, nightly=100))
@given(first=BLOCKERS, words=st.lists(st.text(max_size=6), min_size=1, max_size=5))
def test_rewording_the_next_step_never_earns_a_turn_when_nothing_observed_changed(
    first: Blocker, words: list[str]
) -> None:
    same = [
        first.model_copy(update={"summary": word, "next_step": word, "kind": first.kind})
        for word in words
    ]
    history = (first, *same)
    stop = refusal(history, config(max_unmeasured_turns=len(history) + 1), PARENT)
    assert stop is not None
    assert stop.kind is FailureKind.REPEATED_TURN


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    earlier=st.lists(BLOCKERS, min_size=1, max_size=5),
    fresh=st.one_of(
        st.builds(Blocker, kind=st.just(BlockerKind.FAILED), revision=st.just(revision("new"))),
        st.builds(Blocker, kind=st.just(BlockerKind.NO_REPLY), digest=st.just("new-digest")),
    ),
)
def test_a_turn_that_retained_a_new_revision_or_printed_a_new_failure_earns_another(
    earlier: list[Blocker], fresh: Blocker
) -> None:
    history = (*earlier, fresh)
    assert refusal(history, config(max_unmeasured_turns=len(history) + 1), PARENT) is None


def test_no_turn_taken_yet_means_nothing_to_refuse() -> None:
    assert refusal((), config(), PARENT) is None
