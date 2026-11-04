"""Two requests are the same work when they differ at most in their stages' purposes."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vs_evaluation.api import (
    ContentDigest,
    EvaluationRequest,
    EvaluationStep,
    EvidenceFingerprints,
    EvidenceKind,
    SemanticEvaluationStage,
    evaluation_work_digest,
    same_work,
)

_PURPOSES = st.one_of(st.none(), st.sampled_from(["official", "local-validation", "baseline"]))
_KINDS = st.lists(st.sampled_from(list(EvidenceKind)), min_size=1, max_size=3, unique=True)


def _request(
    kinds: list[EvidenceKind], purpose: str | None, *, snapshot: str = "candidate"
) -> EvaluationRequest:
    digest = ContentDigest.sha256(b"same")
    fingerprints = EvidenceFingerprints(
        candidate=digest, evaluator=digest, workload=digest, environment=digest
    )
    stages = []
    for kind in kinds:
        payload = SemanticEvaluationStage(
            snapshot=snapshot, kind=kind, fingerprints=fingerprints, purpose=purpose
        ).model_dump(mode="json")
        if purpose is None:
            # The format recorded before stages carried a purpose has no such key.
            del payload["purpose"]
        stages.append(EvaluationStep(name=kind.value, payload=payload))
    return EvaluationRequest(key="handle", stages=tuple(stages))


@given(kinds=_KINDS, recorded=_PURPOSES, asked=_PURPOSES)
def test_requests_that_differ_only_in_purpose_are_the_same_work(
    kinds: list[EvidenceKind], recorded: str | None, asked: str | None
) -> None:
    assert same_work(_request(kinds, recorded), _request(kinds, asked))
    assert evaluation_work_digest(_request(kinds, recorded)) == evaluation_work_digest(
        _request(kinds, asked)
    )


@given(kinds=_KINDS, other_kinds=_KINDS, purpose=_PURPOSES)
def test_another_snapshot_or_stage_list_is_other_work(
    kinds: list[EvidenceKind], other_kinds: list[EvidenceKind], purpose: str | None
) -> None:
    request = _request(kinds, purpose)

    assert not same_work(request, _request(kinds, purpose, snapshot="other"))
    assert evaluation_work_digest(request) != evaluation_work_digest(
        _request(kinds, purpose, snapshot="other")
    )
    assert same_work(request, _request(other_kinds, purpose)) == (kinds == other_kinds)


def test_a_payload_that_is_no_semantic_stage_is_compared_as_it_is() -> None:
    def request(payload: dict[str, str]) -> EvaluationRequest:
        return EvaluationRequest(key="k", stages=(EvaluationStep(name="s", payload=payload),))

    assert same_work(request({"purpose": "a"}), request({"purpose": "a"}))
    assert not same_work(request({"purpose": "a"}), request({"purpose": "b"}))
