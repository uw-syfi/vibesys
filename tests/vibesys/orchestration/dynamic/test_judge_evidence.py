"""The judge reviews the candidate with the evaluations its implementer took of it, and no others.

An implementer measures its own workspace through the evaluation tool; core records each result
as trusted `local-validation` evidence of the attempt. The judge, a read-only turn of the same
workstream, is shown exactly those records of the exact candidate (decoded by the evidence owner),
oldest first, so it never asks for the evaluation again (the v13 run was refused for budget and
passed the candidate while saying so). A reviewed round that passed also has its measured metric.
"""

from __future__ import annotations

from itertools import cycle

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.vibesys.orchestration.dynamic._host import (
    PLANNER_LABEL,
    Host,
    begin,
    config,
)
from tests.vibesys.orchestration.dynamic._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.orchestration.dynamic._values import evidence_ref, revision
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.orchestration.dynamic.models import ImplementerResult, ReviewResult
from vibesys.orchestration.dynamic.search.api import InterpretEvidence, ReviewPrompt
from vs_core.api import (
    AttemptId,
    EvidenceKind,
    EvidenceRef,
    Retained,
    RevisionRef,
    Scope,
    StartAttempt,
)
from vs_sim.api.testing import examples

MODES = across_restarts
CANDIDATE = RevisionRef.of_git_commit("abc123")
OTHER = revision("other")


def judge_prompt(host: Host) -> ReviewPrompt:
    context = host.run.expect_turn(role="judge").context
    assert isinstance(context, ReviewPrompt)
    return context


@MODES
@example(records=[(True, True, "local-validation", "trusted")] * 2)
@settings(max_examples=examples(pr=10, nightly=40), deadline=None, derandomize=True, database=None)
@given(
    records=st.lists(
        st.tuples(
            st.booleans(),  # taken in this workstream's attempt
            st.booleans(),  # of the candidate the judge reviews
            st.sampled_from(["local-validation", "official"]),
            st.sampled_from(["trusted", "self-report"]),
        ),
        max_size=6,
    )
)
def test_the_judge_is_shown_each_trusted_evaluation_of_the_candidate_taken_in_its_attempt(
    *, restarting: bool, records: list[tuple[bool, bool, str, str]]
) -> None:
    host = begin(config(), restarting=restarting)
    host.drain_until_turn(PLANNER_LABEL)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(implement("h1")))
    host.answer(host.run.expect(StartAttempt, label="start"))
    (attempt,) = host.opened
    mine = Scope(owner=attempt.attempt_id, generation=attempt.generation)
    foreign = Scope(owner=AttemptId(root="other"), generation=0)
    taken: list[EvidenceRef] = [
        evidence_ref(
            f"e{n}",
            EvidenceKind.BENCHMARK,
            CANDIDATE if same else OTHER,
            scope=mine if own else foreign,
            purpose=purpose,
            provenance=provenance,
            observation_sequence=len(records) - n,
        )
        for n, (own, same, purpose, provenance) in enumerate(records)
    ]
    host.run.view.changes["measurements"] = tuple(taken)

    host.run.reply(
        host.run.expect_turn(role="implementer"),
        ImplementerResult.model_validate_json(implemented()),
        checkpoint=Retained(revision=CANDIDATE),
    )
    host.drain_until_turn("judge")

    shown = [item.evidence_id.root for item in judge_prompt(host).evaluations]
    expected = [
        ref.evidence_id.root
        for ref in sorted(taken, key=lambda ref: ref.observation_sequence)
        if ref.scope == mine
        and ref.candidate == CANDIDATE
        and ref.purpose == "local-validation"
        and ref.provenance == "trusted"
    ]
    assert shown == expected


@MODES
@settings(max_examples=examples(pr=10, nightly=30), deadline=None, derandomize=True, database=None)
@given(
    passes=st.lists(st.booleans(), min_size=1, max_size=3),
    hypotheses=st.integers(1, 2),
    in_flight=st.integers(1, 2),
)
def test_every_round_whose_review_passed_was_measured_and_has_its_metric(
    *, restarting: bool, passes: list[bool], hypotheses: int, in_flight: int
) -> None:
    host = begin(config(max_rounds=2, max_in_flight=in_flight), restarting=restarting)
    host.drain_until_turn(PLANNER_LABEL)
    entries = [implement(f"h{n}") for n in range(min(hypotheses, in_flight))]
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(*entries))
    verdicts = cycle(passes)
    for _ in range(300):
        for asked in list(host.run.open):
            if asked.label == "implementer":
                host.run.reply(
                    asked,
                    ImplementerResult.model_validate_json(implemented()),
                    checkpoint=Retained(revision=RevisionRef.of_git_commit(asked.decision.root)),
                )
            elif asked.label == "judge":
                host.run.reply(
                    asked, ReviewResult.model_validate_json(reviewed(passed=next(verdicts)))
                )
        if not host.step(0):
            break

    record = host.record
    assert record is not None
    rounds = [round_ for item in record.hypotheses for round_ in item.rounds]
    assert rounds
    for round_ in rounds:
        if round_.review_passed:
            assert round_.failure is None, round_.failure
            assert round_.metrics, "a passed review with no measured metric"


def test_interpret_evidence_carries_the_records_it_decodes_and_refuses_none() -> None:
    ref = evidence_ref("a", EvidenceKind.BENCHMARK, CANDIDATE)

    with pytest.raises(ValidationError):
        InterpretEvidence(evidence=())
    assert InterpretEvidence(evidence=(ref,)).evidence == (ref,)
