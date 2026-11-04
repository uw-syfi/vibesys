"""Core-owned rules the dynamic search relies on, each pinned where core owns it.

They were once asserted only through the dynamic strategy's tests; the flow rewrite removed
those, and no vs-core test checked them: the doubling and cap of a lost turn's wait, a result
of another scope keeping no evidence, an evidence id being unique only within its source
request, the checkpoint a turn owns being its own last one, and a suspended turn being a wait
whatever text it ended with.
"""

from __future__ import annotations

from itertools import pairwise
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.testing import FakeView, FlowRun, Prompt, fake_search

from .flow_support import Config, Plan

if TYPE_CHECKING:
    from vs_core.api.testing import Asked

# -- the wait before a lost turn is asked again --------------------------------------------

_BASE = st.floats(min_value=0.01, max_value=600.0)
_CAP = st.floats(min_value=0.01, max_value=3600.0)


@given(base=_BASE, cap=_CAP, drops=st.integers(min_value=0, max_value=40))
def test_the_backoff_starts_at_the_base_never_shrinks_and_never_passes_the_cap(
    base: float, cap: float, drops: int
) -> None:
    first, ceiling = min(base, cap), max(base, cap)
    backoff = core.Backoff(first=first, cap=ceiling)

    waits = [backoff.delay(n) for n in range(drops + 1)]

    assert waits[0] == first
    assert all(0 < wait <= ceiling for wait in waits)
    assert waits == sorted(waits)
    for earlier, later in pairwise(waits):
        assert later == min(ceiling, earlier * 2) or later == ceiling


# -- which evidence a measurement keeps ------------------------------------------------------

SCOPE = core.Scope(owner=core.RunId(root="run"), generation=0)
CANDIDATE = core.RevisionRef(revision_id=core.RevisionId(root="c"), digest="digest-c")


def _ref(name: str, source: str = "request") -> core.EvidenceRef:
    return core.EvidenceRef(
        evidence_id=core.EvidenceId(root=name),
        kind=core.EvidenceKind.BENCHMARK,
        purpose="official",
        scope=SCOPE,
        source_request=core.RequestId(root=source),
        candidate=CANDIDATE,
        observation_sequence=1,
        evaluator_digest="e",
        workload_digest="w",
        environment_digest="n",
        provenance="trusted",
        status=core.ObservationStatus.SUCCEEDED,
    )


@given(
    owner=st.sampled_from(["run", "other"]),
    generation=st.integers(0, 2),
    names=st.lists(st.sampled_from("abcd"), unique=True, max_size=4),
)
def test_a_result_of_another_scope_keeps_nothing_even_when_its_records_carry_the_asked_one(
    owner: str, generation: int, names: list[str]
) -> None:
    other = core.Scope(owner=core.RunId(root=owner), generation=generation)
    result = core.MeasurementResult(
        scope=other,
        evidence=tuple(_ref(name) for name in names),
        status=core.ObservationStatus.SUCCEEDED,
    )

    kept = core.trusted_keys(result, scope=SCOPE, candidate=CANDIDATE, purpose="official")

    assert kept == (() if other != SCOPE else tuple(_ref(name).key for name in names))


@given(name=st.text(alphabet="abc", min_size=1, max_size=3), sources=st.permutations("xyz"))
def test_an_evidence_id_is_unique_only_within_its_source_request(
    name: str, sources: list[str]
) -> None:
    refs = [_ref(name, source) for source in sources]

    assert {ref.evidence_id for ref in refs} == {core.EvidenceId(root=name)}
    assert len({ref.key for ref in refs}) == len(refs)


# -- the candidate a turn retained -------------------------------------------------------------

ATTEMPT = core.AttemptId(root="attempt")


def _invocation(name: str) -> core.InvocationRef:
    return core.InvocationRef(
        session_id=core.SessionId(root="session"),
        invocation_id=core.InvocationId(root=name),
        generation=0,
    )


def _revision(name: str) -> core.RevisionRef:
    return core.RevisionRef(revision_id=core.RevisionId(root=name), digest=f"digest-{name}")


def _checkpoint(invocation: str, name: str) -> core.AttemptCheckpoint:
    return core.AttemptCheckpoint(
        invocation=_invocation(invocation),
        request_id=core.RequestId(root=f"retain-{name}"),
        revision=_revision(name),
        retention="candidate",
    )


def _view(*checkpoints: core.AttemptCheckpoint) -> core.CoreView:
    attempt = core.AttemptView(
        attempt_id=ATTEMPT,
        item_id=core.ItemId(root="item"),
        generation=0,
        phase=core.AttemptPhase.ACTIVE,
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=_revision("candidate")
        ),
        budget=core.AttemptBudget(),
        checkpoints=checkpoints,
    )
    return core.project(core.initial_state()).model_copy(update={"attempts": (attempt,)})


@given(
    st.lists(st.tuples(st.sampled_from("abc"), st.integers(0, 9)), max_size=8),
    st.sampled_from("abcd"),
)
def test_a_turn_owns_only_its_own_last_checkpoint(
    retained: list[tuple[str, int]], asked: str
) -> None:
    view = _view(*(_checkpoint(inv, f"r{n}-{i}") for i, (inv, n) in enumerate(retained)))
    mine = [f"r{n}-{i}" for i, (inv, n) in enumerate(retained) if inv == asked]

    kept = core.turn_candidate(view, ATTEMPT, _invocation(asked), predecessors=())
    resolved = core.TurnChain[core.Value]().candidate_outcome(view, ATTEMPT, _invocation(asked))

    if mine:
        assert kept == _revision(mine[-1])
        assert isinstance(resolved, core.TurnCandidateRetained)
        assert resolved.revision == _revision(mine[-1])
    else:
        assert kept is None
        assert isinstance(resolved, core.TurnCandidatePending)


def test_a_turn_of_an_unknown_attempt_has_no_candidate() -> None:
    kept = core.turn_candidate(
        _view(), core.AttemptId(root="none"), _invocation("x"), predecessors=()
    )
    assert kept is None


@given(
    steps=st.lists(
        st.tuples(st.sampled_from(("suspend", "correct", "lose", "retry")), st.booleans()),
        max_size=10,
    ),
    first_retains=st.booleans(),
)
def test_a_turn_chains_candidate_is_only_the_current_invocations_outcome(
    steps: list[tuple[str, bool]], *, first_retains: bool
) -> None:
    """Older turns never decide the current turn, whatever transitions produced them."""
    chain: core.TurnChain[core.Value] = core.TurnChain()
    current = 0
    retained = {0: first_retains}
    for kind, retains in steps:
        ref = _invocation(f"t{current}")
        nxt = current + 1
        if kind == "suspend":
            chain = chain.suspended(ref, core.ContinuationId(root=f"c{current}")).resumed(
                authorized=core.InvocationId(root=f"t{nxt}"),
                continuation=core.ContinuationId(root=f"c{current}"),
                context=None,
            )
        elif kind == "correct":
            corrected = chain.corrected(ref, None, limit=100)
            assert corrected is not None
            chain = corrected
        elif kind == "lose":
            again = chain.asked_again(ref, now=0.0, limit=100, backoff=0.0)
            if again is None:
                continue
            chain = again
        else:
            chain = chain.followed_by(charge="paid")
        current = nxt
        retained[current] = retains
    view = _view(
        *(_checkpoint(f"t{turn}", f"r{turn}") for turn in sorted(retained) if retained[turn])
    )

    candidate = chain.candidate_outcome(view, ATTEMPT, _invocation(f"t{current}"))

    if retained[current]:
        assert isinstance(candidate, core.TurnCandidateRetained)
        assert candidate.revision == _revision(f"r{current}")
    else:
        assert isinstance(candidate, core.TurnCandidatePending)


# -- a turn that core recorded as suspended is a wait whatever it said ----------------------------

EXCHANGES = core.Exchanges(correction="c.j2", resume="r.j2", wait_unrecorded="w.j2")
WAITER = core.Role(
    name="waiter",
    title="Waiter",
    prompt="waiter.j2",
    reply=Plan,
    workspace=None,
    access=core.Access.READ_ONLY,
    turn_time=600.0,
    corrections=1,
    lost_turns=1,
    charge=core.Charge.TURN,
    may_wait_for_evaluations=True,
    exchanges=EXCHANGES,
)
SUSPENDING = FakeView(
    changes={"capabilities": core.Capabilities(lifecycle=frozenset({"suspend"}), operations=())}
)


class Ended(core.Value):
    """How the one ask ended."""

    kind: str


def _start(*, restarting: bool) -> FlowRun[Config]:
    async def root(ctx: core.Flow, config: Config) -> None:
        del config
        result = await ctx.agents.session(WAITER).turn(Prompt(text="go"))
        ctx.publish(Ended(kind=type(result).__name__))

    search = fake_search("waits", Config, (WAITER,))(root)
    return FlowRun.start(search, Config(), view=SUSPENDING, restarting=restarting)


def _turns(run: FlowRun[Config]) -> list[Asked]:
    return [item for item in run.open if isinstance(item.request, core.RequestTurn)]


def _suspended_with(run: FlowRun[Config], asked: Asked, output: str) -> core.Continuation:
    """The turn ended suspended and core recorded the wait, whatever text the agent left."""
    request = asked.request
    assert isinstance(request, core.RequestTurn)
    spec = request.turn
    invocation = core.InvocationRef(
        session_id=spec.session.session_id, invocation_id=spec.invocation_id, generation=0
    )
    run.send(
        core.TurnEnded(
            decision=asked.decision,
            result=core.TurnResult(
                invocation=invocation,
                observation=core.Observation(
                    event_id=core.EventId(root="event"),
                    request_id=core.RequestId(root="request"),
                    scope=request.scope,
                    sequence=1,
                    observed_at=0.0,
                    status=core.ObservationStatus.SUCCEEDED,
                    accepted=True,
                    terminal=True,
                    released=True,
                ),
                output_schema=spec.output_schema,
                output_json=output,
                suspending=True,
            ),
        )
    )
    continuation = core.Continuation(
        continuation_id=core.ContinuationId(root="c"),
        invocation=invocation,
        next_invocation=invocation.model_copy(
            update={"invocation_id": core.InvocationId(root="resume")}
        ),
        jobs=(),
        deadline_at=1000.0,
        phase=core.ContinuationPhase.WAITING,
    )
    run.send(core.TurnYielded(continuation=continuation))
    return continuation


@given(
    output=st.one_of(
        st.just('{"n": 1}'),
        st.just("not json"),
        st.just('{"kind": "waiting_for_evaluation", "handles": ["h"]}'),
        st.text(max_size=20),
    )
)
@pytest.mark.parametrize("restarting", [False, True], ids=["live", "restart"])
def test_a_turn_core_recorded_as_suspended_is_resumed_whatever_text_it_ended_with(
    output: str, *, restarting: bool
) -> None:
    run = _start(restarting=restarting)
    (asked,) = _turns(run)

    continuation = _suspended_with(run, asked, output)
    # The suspended turn is not answered again (the host's own bookkeeping keeps its request
    # listed): no correction is asked of it, whatever its text was.
    assert _turns(run) == [asked]

    run.resume_ready(continuation)
    (_, follow) = _turns(run)
    resumed = follow.request
    assert isinstance(resumed, core.RequestTurn)
    assert resumed.turn.charge_class == "resume"
    assert resumed.turn.invocation_id == continuation.next_invocation.invocation_id
