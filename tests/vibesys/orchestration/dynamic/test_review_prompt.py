"""The judge is told every trusted evaluation of the candidate it reviews, or that none exists.

`test_review_evidence.py` pins the typed list built from records and readings. These tests pin
the two ends of it: the review flow reading the evaluations the author took (the records core
holds, decoded by the evidence owner) into the judge's context, and the judge's prompt, which
must not let an evaluation's output close the fence it is shown in and must not let the judge fail
a claim it has no measurement for (LIVE-2: the judge said no evidence was provided after the
implementer linked it).
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._host import PLANNER_LABEL, begin, config
from tests.vibesys.orchestration.dynamic._replies import implement, implemented, plan_reply
from tests.vibesys.orchestration.dynamic.core_policy._contexts import CONTEXTS, render
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.orchestration.dynamic.models import ImplementerResult
from vibesys.orchestration.dynamic.search.api import PromptTemplate as Template
from vibesys.orchestration.dynamic.search.api import (
    ReviewEvaluation,
    ReviewPrompt,
)
from vs_core.api import (
    EvidenceId,
    EvidenceKind,
    EvidenceRef,
    ObservationStatus,
    Operation,
    RequestId,
    Retained,
    RevisionRef,
    Scope,
    StartAttempt,
)
from vs_sim.api.testing import examples

MODES = across_restarts
CANDIDATE = RevisionRef.of_git_commit("c1")
THROUGHPUT = 123.0
STATUSES = st.sampled_from([ObservationStatus.SUCCEEDED, ObservationStatus.FAILED])


def taken(name: str, sequence: int, status: ObservationStatus, scope: Scope) -> EvidenceRef:
    """A trusted evaluation the author took of the candidate in its own workspace."""
    return EvidenceRef(
        evidence_id=EvidenceId(root=name),
        kind=EvidenceKind.BENCHMARK,
        purpose="local-validation",
        scope=scope,
        source_request=RequestId(root=f"request-{name}"),
        candidate=CANDIDATE,
        observation_sequence=sequence,
        evaluator_digest="e",
        workload_digest="w",
        environment_digest="n",
        provenance="trusted",
        status=status,
    )


@MODES
@settings(max_examples=examples(pr=10, nightly=12), deadline=None, derandomize=True, database=None)
@given(statuses=st.lists(STATUSES, max_size=4))
def test_the_judge_is_given_the_id_and_verdict_of_every_evaluation_the_author_took(
    *, restarting: bool, statuses: list[ObservationStatus]
) -> None:
    host = begin(config(), restarting=restarting)
    host.drain_until_turn(PLANNER_LABEL)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(implement("h1")))
    (opened,) = host.open(StartAttempt)
    host.answer(opened)
    assert isinstance(opened.request, StartAttempt)
    scope = Scope(owner=opened.request.attempt_id, generation=0)
    refs = tuple(taken(f"local-{n}", n, status, scope) for n, status in enumerate(statuses, 1))
    # core holds the records however they arrived; the judge reads them in the order taken
    host.run.view.changes["measurements"] = tuple(reversed(refs))
    result = ImplementerResult.model_validate_json(implemented())
    host.run.reply(
        host.run.expect_turn(role="implementer"), result, checkpoint=Retained(revision=CANDIDATE)
    )
    for decoding in host.open(Operation):
        host.answer(decoding)  # the evidence owner decodes the records

    context = host.run.expect_turn(role="judge").context
    assert isinstance(context, ReviewPrompt)
    assert context.candidate == CANDIDATE
    assert [item.evidence_id.root for item in context.evaluations] == [
        ref.evidence_id.root for ref in refs
    ]
    for item, ref in zip(context.evaluations, refs, strict=True):
        passed = ref.status is ObservationStatus.SUCCEEDED
        assert (item.kind, item.revision, item.status) == (ref.kind, CANDIDATE, ref.status)
        assert item.passed is passed
    text = render(Template.REVIEW, context)
    assert ("no trusted evaluation of this candidate exists yet" in text) is (not refs)
    for item in context.evaluations:
        assert item.evidence_id.root in text
    assert text.count("PASS") == statuses.count(ObservationStatus.SUCCEEDED)
    assert text.count("FAIL") == statuses.count(ObservationStatus.FAILED)


def fence_lines(text: str) -> list[int]:
    """The indexes of the lines that are only backticks (three or more): fences."""
    lines = [line.strip() for line in text.splitlines()]
    return [i for i, line in enumerate(lines) if line and set(line) == {"`"} and len(line) >= 3]


@settings(max_examples=examples(pr=8, nightly=100))
@given(tail=st.text(alphabet="`ab\n ", max_size=60), data=st.data())
def test_a_failure_text_cannot_close_its_fence(tail: str, data: st.DataObject) -> None:
    base = data.draw(CONTEXTS[Template.REVIEW])
    assert isinstance(base, ReviewPrompt)
    feedback = "x" + tail
    shown = ReviewEvaluation(
        evidence_id=EvidenceId(root="e"),
        kind=EvidenceKind.BENCHMARK,
        revision=base.candidate,
        status=ObservationStatus.SUCCEEDED,
        passed=False,
        feedback=feedback,
    )
    context = base.model_copy(update={"evaluations": (shown,)})
    lines = render(Template.REVIEW, context).splitlines()
    opening = next(i for i, line in enumerate(lines) if "Unverified program output" in line)
    fences = [i for i in fence_lines("\n".join(lines)) if i > opening]
    start, end = fences[0], fences[1]
    assert start == opening + 1
    body = lines[start + 1 : end]
    assert "".join("".join(body).split()) == "".join(feedback.replace("`", "'").split())


@settings(max_examples=examples(pr=8, nightly=100))
@given(data=st.data())
def test_the_judge_may_not_fail_a_claim_it_has_no_measurement_for(data: st.DataObject) -> None:
    """The rubric requires trusted evidence before a performance verdict (LIVE-2 finding 4)."""
    context = data.draw(CONTEXTS[Template.REVIEW])
    offered = " ".join(render(Template.REVIEW, context, agent_evaluation=True).split())
    withheld = " ".join(render(Template.REVIEW, context, agent_evaluation=False).split())

    assert "fail the candidate on such a claim only when a listed result shows it" in offered
    assert "measure the workspace once with `submit_evaluation`" in offered
    assert "fail the candidate on such a claim only when a listed result shows it" in withheld
    assert "do not fail the candidate for the missing measurement" in withheld
    assert "measure the workspace once with `submit_evaluation`" not in withheld
