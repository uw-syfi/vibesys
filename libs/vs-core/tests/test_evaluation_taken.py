"""A flow sees the evaluations an agent took of a revision itself: this attempt's, trusted, oldest first.

An agent measures its own workspace through the evaluation tool; core records each result as
trusted ``local-validation`` evidence of the attempt. ``ctx.evaluation.taken`` is how a flow
(a judge's review, say) finds them, so it must never offer another attempt's, a stale
generation's, another revision's, an official or an untrusted record.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.testing import FakeView

from .flow_support import Config, open_workspace, start

CANDIDATE = core.RevisionRef.of_git_commit("candidate")
OTHER = core.RevisionRef.of_git_commit("other")
ATTEMPT = core.AttemptId(root="at")
GENERATION = 0


class Seen(core.Value):
    """The evidence IDs the flow was offered."""

    ids: tuple[str, ...]


def record(name: str, sequence: int = 1, **changes: object) -> core.EvidenceRef:
    """A trusted local-validation record of the candidate taken by the attempt under test."""
    ref = core.EvidenceRef(
        evidence_id=core.EvidenceId(root=name),
        kind=core.EvidenceKind.BENCHMARK,
        purpose="local-validation",
        scope=core.Scope(owner=ATTEMPT, generation=GENERATION),
        source_request=core.RequestId(root=f"request-{name}"),
        candidate=CANDIDATE,
        observation_sequence=sequence,
        evaluator_digest="e",
        workload_digest="w",
        environment_digest="n",
        provenance="trusted",
        status=core.ObservationStatus.SUCCEEDED,
    )
    return ref.model_copy(update=changes)


FOREIGN = {
    "another attempt": {"scope": core.Scope(owner=core.AttemptId(root="x"), generation=0)},
    "stale generation": {"scope": core.Scope(owner=ATTEMPT, generation=GENERATION + 1)},
    "another revision": {"candidate": OTHER},
    "official": {"purpose": "official"},
    "untrusted": {"provenance": "self-report"},
}


def offered(measurements: tuple[core.EvidenceRef, ...]) -> tuple[str, ...]:
    """The evidence IDs a flow is offered by ``taken`` for the attempt and revision under test."""

    async def review(ctx: core.Flow, _config: Config) -> None:
        async with open_workspace(ctx, ATTEMPT.root) as workspace:
            assert isinstance(workspace, core.Workspace)
            found = ctx.evaluation.taken(workspace, CANDIDATE)
            ctx.publish(Seen(ids=tuple(item.evidence_id.root for item in found)))

    view = FakeView(changes={"measurements": measurements})
    run = start(review, view=view)
    run.open_workspace(run.expect(core.StartAttempt))
    seen = run.published(Seen)
    assert seen is not None
    return seen.ids


@given(
    sequences=st.lists(st.integers(1, 50), unique=True, max_size=6),
    flaws=st.lists(st.sampled_from(sorted(FOREIGN)), max_size=5),
)
def test_only_this_attempts_current_trusted_local_evidence_of_the_revision_is_offered_oldest_first(
    sequences: list[int], flaws: list[str]
) -> None:
    mine = [record(f"mine-{n}", n) for n in sequences]
    foreign = [record(f"foreign-{i}", 1, **FOREIGN[name]) for i, name in enumerate(flaws)]
    both = [*foreign, *mine]
    assert offered(tuple(both)) == tuple(f"mine-{n}" for n in sorted(sequences))


def last_evaluated(measurements: tuple[core.EvidenceRef, ...]) -> core.RevisionRef | None:
    """The revision ``last_evaluated`` names for the attempt under test."""

    class Seen(core.Value):
        revision: core.RevisionRef | None

    async def review(ctx: core.Flow, _config: Config) -> None:
        async with open_workspace(ctx, ATTEMPT.root) as workspace:
            assert isinstance(workspace, core.Workspace)
            ctx.publish(Seen(revision=ctx.evaluation.last_evaluated(workspace)))

    run = start(review, view=FakeView(changes={"measurements": measurements}))
    run.open_workspace(run.expect(core.StartAttempt))
    seen = run.published(Seen)
    assert seen is not None
    return seen.revision


NOT_THIS_ATTEMPTS = sorted(set(FOREIGN) - {"another revision"})


@given(
    sequences=st.lists(st.integers(1, 50), unique=True, min_size=1, max_size=6),
    flaws=st.lists(st.sampled_from(NOT_THIS_ATTEMPTS), max_size=4),
)
def test_the_last_evaluated_revision_is_where_this_attempts_newest_conclusive_evaluation_was_taken(
    sequences: list[int], flaws: list[str]
) -> None:
    newest = max(sequences)
    mine = [
        record(f"mine-{n}", n, candidate=CANDIDATE if n == newest else OTHER) for n in sequences
    ]
    # records newer than all of these that are not this attempt's, or did not conclude, name none
    unfinished = record("unfinished", 100, candidate=OTHER, status=core.ObservationStatus.FAILED)
    foreign = [
        record(f"foreign-{i}", 200 + i, **{**FOREIGN[name], "candidate": OTHER})
        for i, name in enumerate(flaws)
    ]
    assert last_evaluated((*foreign, unfinished, *mine)) == CANDIDATE


def test_a_workspace_nobody_evaluated_has_no_last_evaluated_revision() -> None:
    assert last_evaluated(()) is None
