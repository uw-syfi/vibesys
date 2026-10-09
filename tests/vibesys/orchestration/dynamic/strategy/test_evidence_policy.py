"""Which evidence, readings and checkpoints the strategy trusts (review findings #7 and #9)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.vibesys.orchestration.dynamic.strategy._run import config
from tests.vibesys.orchestration.dynamic.strategy._views import (
    empty_view,
    evidence_ref,
    revision,
)

from vibesys.orchestration.dynamic.strategy.api import (
    AcceptedReading,
    DynamicStrategy,
    EvidenceReading,
    EvidenceReadings,
    InterpretEvidence,
    RetainVerifiedRevision,
    accept_readings,
    ledger_refs,
    trusted_keys,
    turn_candidate,
)
from vs_core.api import (
    AttemptBudget,
    AttemptCheckpoint,
    AttemptId,
    AttemptPhase,
    AttemptView,
    EvidenceId,
    EvidenceKind,
    EvidenceRef,
    IntentBlocked,
    InvocationId,
    InvocationRef,
    ItemId,
    MeasurementResult,
    ObservationStatus,
    RequestId,
    RunId,
    RunView,
    Scope,
    SessionId,
    WorkspaceMode,
    WorkspacePlan,
)

if TYPE_CHECKING:
    from random import Random

SCOPE = Scope(owner=RunId(root="run"), generation=0)
CANDIDATE = revision("candidate")


def _ref(name: str, kind: EvidenceKind = EvidenceKind.BENCHMARK, **changes: object) -> EvidenceRef:
    ref = evidence_ref(name, kind, CANDIDATE).model_copy(update={"purpose": "official"})
    return ref.model_copy(update=changes)


def _reading(ref: EvidenceRef, *, passed: bool = True) -> EvidenceReading:
    return EvidenceReading(
        evidence_id=ref.evidence_id, kind=ref.kind, passed=passed, stage=ref.kind.value
    )


def _result(*evidence: EvidenceRef, source: str | None = None) -> MeasurementResult:
    return MeasurementResult(
        scope=SCOPE,
        source_request=None if source is None else RequestId(root=source),
        evidence=evidence,
        status=ObservationStatus.SUCCEEDED,
    )


# -- #9: evidence is accepted only for the right measurement ----------------------------


def test_trusted_evidence_of_the_measured_candidate_is_kept() -> None:
    ref = _ref("a")
    keys = trusted_keys(_result(ref), scope=SCOPE, candidate=CANDIDATE, purpose="official")
    assert keys == (ref.key,)


@pytest.mark.parametrize(
    "changes",
    [
        {"provenance": "self-report"},
        {"candidate": revision("other")},
        {"purpose": "baseline"},
        {"scope": Scope(owner=RunId(root="run"), generation=1)},
        {"source_request": RequestId(root="foreign")},
    ],
    ids=["untrusted", "other-candidate", "other-purpose", "other-generation", "other-source"],
)
def test_evidence_that_is_not_this_measurements_is_dropped(changes: dict[str, object]) -> None:
    result = _result(_ref("a").model_copy(update=changes), source="request-a")
    assert trusted_keys(result, scope=SCOPE, candidate=CANDIDATE, purpose="official") == ()


def test_a_result_for_another_generation_keeps_nothing() -> None:
    other = Scope(owner=RunId(root="run"), generation=1)
    result = MeasurementResult(
        scope=other, evidence=(_ref("a"),), status=ObservationStatus.SUCCEEDED
    )
    assert trusted_keys(result, scope=SCOPE, candidate=CANDIDATE, purpose="official") == ()


def test_the_ledger_must_hold_every_requested_record() -> None:
    held = _ref("a")
    view = empty_view().model_copy(update={"measurements": (held,)})
    assert ledger_refs(view, (held.key,)) == (held,)
    assert ledger_refs(view, (held.key, _ref("b").key)) is None


def _accept(requested: tuple[EvidenceRef, ...], *readings: EvidenceReading) -> object:
    return accept_readings(EvidenceReadings(status="succeeded", readings=readings), requested)


def test_a_reading_for_an_unrequested_id_is_refused() -> None:
    asked, stray = _ref("asked"), _ref("stray")
    assert isinstance(_accept((asked,), _reading(asked), _reading(stray)), str)


def test_a_reading_that_two_requested_records_share_is_ambiguous() -> None:
    first = _ref("same", source_request=RequestId(root="one"))
    second = _ref("same", source_request=RequestId(root="two"))
    assert isinstance(_accept((first, second), _reading(first)), str)


def test_a_reading_of_the_wrong_kind_is_refused() -> None:
    ref = _ref("a", EvidenceKind.BENCHMARK)
    wrong = EvidenceReading(
        evidence_id=ref.evidence_id, kind=EvidenceKind.CORRECTNESS, passed=True, stage="x"
    )
    assert isinstance(_accept((ref,), wrong), str)


def test_a_pass_over_a_failed_record_is_refused() -> None:
    ref = _ref("a", status=ObservationStatus.FAILED)
    assert isinstance(_accept((ref,), _reading(ref, passed=True)), str)
    assert not isinstance(_accept((ref,), _reading(ref, passed=False)), str)


def test_a_repeated_reading_is_refused() -> None:
    ref = _ref("a")
    assert isinstance(_accept((ref,), _reading(ref), _reading(ref)), str)


@given(st.lists(st.sampled_from("abcdef"), unique=True, min_size=1), st.randoms())
def test_accepted_readings_bind_to_exactly_the_requested_records(
    names: list[str], random: Random
) -> None:
    """Any order of readings over distinct requested records binds each to its own source."""
    refs = tuple(_ref(name, source_request=RequestId(root=f"request-{name}")) for name in names)
    readings = [_reading(ref) for ref in refs]
    random.shuffle(readings)
    accepted = _accept(refs, *readings)
    assert isinstance(accepted, tuple)
    assert {item.key for item in accepted if isinstance(item, AcceptedReading)} == {
        ref.key for ref in refs
    }


# -- #7: a retry turn that retained nothing retained no changed candidate ---------------


def _invocation(name: str) -> InvocationRef:
    return InvocationRef(
        session_id=SessionId(root="session"),
        invocation_id=InvocationId(root=name),
        generation=0,
    )


def _checkpoint(invocation: str, name: str) -> AttemptCheckpoint:
    return AttemptCheckpoint(
        invocation=_invocation(invocation),
        request_id=RequestId(root=f"retain-{name}"),
        revision=revision(name),
        retention="candidate",
    )


def _view(*checkpoints: AttemptCheckpoint) -> RunView:
    attempt = AttemptView(
        attempt_id=AttemptId(root="attempt"),
        item_id=ItemId(root="item"),
        generation=0,
        phase=AttemptPhase.ACTIVE,
        workspace=WorkspacePlan(mode=WorkspaceMode.EXCLUSIVE_ROOT, base=CANDIDATE),
        budget=AttemptBudget(),
        checkpoints=checkpoints,
    )
    return empty_view().model_copy(update={"attempts": (attempt,)})


def test_a_retry_turn_that_retained_nothing_is_not_credited_with_an_older_checkpoint() -> None:
    view = _view(_checkpoint("first", "r1"))
    assert turn_candidate(view, AttemptId(root="attempt"), _invocation("retry")) is None
    assert turn_candidate(view, AttemptId(root="attempt"), _invocation("first")) == revision("r1")


@given(
    st.lists(st.tuples(st.sampled_from("abc"), st.integers(0, 9)), max_size=8),
    st.sampled_from("abcd"),
)
def test_a_turn_owns_only_its_own_last_checkpoint(
    retained: list[tuple[str, int]], asked: str
) -> None:
    view = _view(*(_checkpoint(inv, f"r{n}-{i}") for i, (inv, n) in enumerate(retained)))
    mine = [f"r{n}-{i}" for i, (inv, n) in enumerate(retained) if inv == asked]
    expected = revision(mine[-1]) if mine else None
    assert turn_candidate(view, AttemptId(root="attempt"), _invocation(asked)) == expected


def test_a_turn_of_an_unknown_attempt_has_no_candidate() -> None:
    assert turn_candidate(empty_view(), AttemptId(root="none"), _invocation("x")) is None


# -- the contract shapes the strategy relies on -----------------------------------------


def test_interpret_evidence_carries_the_evidence_it_decodes() -> None:
    with pytest.raises(ValidationError):
        InterpretEvidence(evidence=())
    ref = _ref("a")
    assert InterpretEvidence(evidence=(ref,)).evidence == (ref,)


def test_retain_verified_revision_names_the_revision_and_its_accuracy_proof() -> None:
    with pytest.raises(ValidationError):
        RetainVerifiedRevision.model_validate({})
    proof = _ref("proof", EvidenceKind.CORRECTNESS)
    request = RetainVerifiedRevision(revision=CANDIDATE, accuracy_proof=proof)
    assert request.accuracy_proof == proof


def test_the_declaration_offers_suspension_and_profile_capture_as_optional() -> None:
    declaration = DynamicStrategy(config=config()).declaration
    assert {"suspend", "profile-capture"} <= declaration.optional
    assert not declaration.required


def test_a_blocked_intent_nobody_owns_changes_nothing() -> None:
    strategy = DynamicStrategy(config=config())
    event = IntentBlocked(
        request_id=RequestId(root="stranger"),
        target=RequestId(root="target"),
        scope=SCOPE,
        diagnostic="blocked",
    )
    assert strategy.on_event(empty_view(), event) == strategy.state


def test_evidence_ids_are_unique_only_within_a_source_request() -> None:
    one = _ref("same", source_request=RequestId(root="one"))
    two = _ref("same", source_request=RequestId(root="two"))
    assert one.evidence_id == EvidenceId(root="same") == two.evidence_id
    assert one.key != two.key
