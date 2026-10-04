"""Required episodes, canonical requests and source sequences never prove by absence."""

import pytest
from hypothesis import given

import vs_core.api as core
from vs_core.api.proofs import (
    Mismatch,
    Missing,
    ProofField,
    ProofReason,
    Proven,
    current_admission,
    fresh_observation,
    observation_for,
    request_matches,
    resolved_observation,
)

from .proof_facts import admission_facts, observation_facts, request_facts


@pytest.mark.parametrize(
    ("variant", "denial"),
    [
        ("exact", None),
        ("absent", Missing(ProofReason.ABSENT_DECLARATION)),
        ("episode", Missing(ProofReason.ABSENT_EPISODE)),
        ("owner_episode", Missing(ProofReason.ABSENT_EPISODE)),
        ("scope", Mismatch(ProofField.SCOPE)),
        ("generation", Mismatch(ProofField.GENERATION)),
        ("admission", Mismatch(ProofField.ADMISSION_ID)),
    ],
)
@given(admission_facts())
def test_current_admission(
    variant: str,
    denial: Missing | Mismatch | None,
    facts: tuple[core.AttemptView, core.Scope, core.DecisionId],
) -> None:
    attempt, scope, episode = facts
    match variant:
        case "absent":
            attempt = None
        case "episode":
            episode = None
        case "owner_episode":
            attempt = attempt.model_copy(update={"admission_id": None})
        case "scope":
            scope = scope.model_copy(update={"owner": core.AttemptId(root="other")})
        case "generation":
            scope = scope.model_copy(update={"generation": scope.generation + 1})
        case "admission":
            episode = core.DecisionId(root="other")
    assert current_admission(attempt, scope, episode) == (
        Proven(episode) if denial is None else denial
    )


@pytest.mark.parametrize(
    ("variant", "denial"),
    [
        ("exact", None),
        ("intent", Missing(ProofReason.ABSENT_REQUEST)),
        ("request", Missing(ProofReason.ABSENT_REQUEST)),
        ("id", Missing(ProofReason.ABSENT_REQUEST)),
        ("episode", Missing(ProofReason.ABSENT_EPISODE)),
        ("request_id", Mismatch(ProofField.REQUEST_ID)),
        ("scope", Mismatch(ProofField.SCOPE)),
        ("generation", Mismatch(ProofField.GENERATION)),
        ("admission", Mismatch(ProofField.ADMISSION_ID)),
        ("payload", Mismatch(ProofField.PAYLOAD)),
        ("digest", Mismatch(ProofField.DIGEST)),
        ("lifecycle", Mismatch(ProofField.LIFECYCLE)),
    ],
)
@given(request_facts())
def test_request_requires_canonical_facts(
    variant: str, denial: Missing | Mismatch | None, facts: tuple[core.InspectRequest, core.Intent]
) -> None:
    request, intent = facts
    expected = request
    intent, expected = _request_variant(variant, intent, expected)
    assert request_matches(intent, expected) == (Proven(intent) if denial is None else denial)


def _request_variant(
    variant: str, intent: core.Intent, expected: core.InspectRequest
) -> tuple[core.Intent | None, core.InspectRequest | None]:
    match variant:
        case "intent":
            return None, expected
        case "request":
            return intent, None
        case "id":
            expected = expected.model_copy(update={"request_id": None})
        case "episode":
            expected = expected.model_copy(update={"admission_id": None})
        case "request_id":
            expected = expected.model_copy(update={"request_id": core.RequestId(root="other")})
        case "digest":
            intent = intent.model_copy(update={"payload_digest": "other"})
        case "lifecycle":
            intent = intent.model_copy(update={"lifecycle": core.LifecycleClass.IDEMPOTENT_WRITE})
        case _:
            expected = _request_payload_variant(variant, expected)
    return intent, expected


def _request_payload_variant(variant: str, expected: core.InspectRequest) -> core.InspectRequest:
    match variant:
        case "scope":
            return expected.model_copy(
                update={
                    "scope": expected.scope.model_copy(
                        update={"owner": core.AttemptId(root="other")}
                    )
                }
            )
        case "generation":
            return expected.model_copy(
                update={
                    "scope": expected.scope.model_copy(
                        update={"generation": expected.scope.generation + 1}
                    )
                }
            )
        case "admission":
            return expected.model_copy(update={"admission_id": core.DecisionId(root="other")})
        case "payload":
            return expected.model_copy(update={"deadline_at": expected.deadline_at + 1})
        case _:
            return expected


@pytest.mark.parametrize(
    ("variant", "denial"),
    [
        ("exact", None),
        ("intent", Missing(ProofReason.ABSENT_REQUEST)),
        ("observation", Missing(ProofReason.ABSENT_OBSERVATION)),
        ("episode", Missing(ProofReason.ABSENT_EPISODE)),
        ("owner_episode", Missing(ProofReason.ABSENT_EPISODE)),
        ("request_id", Mismatch(ProofField.REQUEST_ID)),
        ("scope", Mismatch(ProofField.SCOPE)),
        ("generation", Mismatch(ProofField.GENERATION)),
        ("admission", Mismatch(ProofField.ADMISSION_ID)),
    ],
)
@given(observation_facts())
def test_observation_requires_recorded_episode(
    variant: str, denial: Missing | Mismatch | None, facts: tuple[core.Intent, core.Observation]
) -> None:
    intent, observation = facts
    match variant:
        case "intent":
            intent = None
        case "observation":
            observation = None
        case "owner_episode":
            intent = intent.model_copy(
                update={"request": intent.request.model_copy(update={"admission_id": None})}
            )
        case _:
            observation = _observation_variant(variant, observation)
    assert observation_for(intent, observation) == (
        Proven(observation) if denial is None else denial
    )


def _observation_variant(variant: str, observation: core.Observation) -> core.Observation:
    match variant:
        case "episode":
            return observation.model_copy(update={"admission_id": None})
        case "request_id":
            return observation.model_copy(update={"request_id": core.RequestId(root="other")})
        case "scope":
            return observation.model_copy(
                update={
                    "scope": observation.scope.model_copy(
                        update={"owner": core.AttemptId(root="other")}
                    )
                }
            )
        case "generation":
            return observation.model_copy(
                update={
                    "scope": observation.scope.model_copy(
                        update={"generation": observation.scope.generation + 1}
                    )
                }
            )
        case "admission":
            return observation.model_copy(update={"admission_id": core.DecisionId(root="other")})
        case _:
            return observation


@pytest.mark.parametrize(
    ("variant", "denial"),
    [
        ("exact", None),
        ("new", None),
        ("first", None),
        ("other_source", None),
        ("absent", Missing(ProofReason.ABSENT_OBSERVATION)),
        ("incomplete", Missing(ProofReason.INCOMPLETE_HISTORY)),
        ("older", Mismatch(ProofField.SEQUENCE)),
        ("conflict", Mismatch(ProofField.SEQUENCE)),
        ("history_conflict", Mismatch(ProofField.SEQUENCE)),
        ("old_conflict", Mismatch(ProofField.SEQUENCE)),
        ("scope", Mismatch(ProofField.SCOPE)),
        ("generation", Mismatch(ProofField.GENERATION)),
        ("admission", Mismatch(ProofField.ADMISSION_ID)),
        ("episode", Missing(ProofReason.ABSENT_EPISODE)),
    ],
)
@given(observation_facts())
def test_fresh_observation_source_history(
    variant: str, denial: Missing | Mismatch | None, facts: tuple[core.Intent, core.Observation]
) -> None:
    _, incoming = facts
    history = (incoming,)
    complete = variant != "incomplete"
    match variant:
        case "new":
            incoming = incoming.model_copy(update={"sequence": incoming.sequence + 1})
        case "first":
            history = ()
        case "other_source":
            history = (incoming.model_copy(update={"request_id": core.RequestId(root="other")}),)
        case "absent":
            incoming = None
        case "older":
            history = (incoming.model_copy(update={"sequence": incoming.sequence + 1}),)
        case "conflict":
            history = (incoming.model_copy(update={"diagnostic": "conflict"}),)
        case "history_conflict":
            history = (incoming, incoming.model_copy(update={"diagnostic": "conflict"}))
        case "old_conflict":
            incoming = incoming.model_copy(update={"sequence": incoming.sequence + 1})
            prior = incoming.model_copy(update={"sequence": incoming.sequence - 1})
            history = (prior, prior.model_copy(update={"diagnostic": "conflict"}), incoming)
        case "scope" | "generation" | "admission" | "episode":
            incoming = _observation_variant(variant, incoming).model_copy(
                update={"sequence": incoming.sequence + 1}
            )
    assert fresh_observation(history, incoming, complete=complete) == (
        Proven(incoming) if denial is None else denial
    )


@pytest.mark.parametrize(
    ("variant", "denial"),
    [
        ("exact", None),
        ("negative", None),
        ("absent", Missing(ProofReason.ABSENT_OBSERVATION)),
        ("nonterminal", Missing(ProofReason.UNRESOLVED)),
        ("unreleased", Missing(ProofReason.UNRESOLVED)),
        ("unknown", Missing(ProofReason.UNRESOLVED)),
        ("pending", Missing(ProofReason.UNRESOLVED)),
        ("manifest", Missing(ProofReason.INCOMPLETE_MANIFEST)),
    ],
)
@given(observation_facts())
def test_resolved_observation_semantics(
    variant: str,
    denial: Missing | Mismatch | None,
    facts: tuple[core.Intent, core.Observation],
) -> None:
    _, observation = facts
    updates = {
        "negative": {"accepted": False, "status": core.ObservationStatus.CANCELLED},
        "nonterminal": {"terminal": False},
        "unreleased": {"released": False},
        "unknown": {"status": core.ObservationStatus.UNKNOWN},
        "pending": {"status": core.ObservationStatus.PENDING},
        "manifest": {"children_complete": False},
    }.get(variant, {})
    observation = None if variant == "absent" else observation.model_copy(update=updates)
    assert resolved_observation(observation) == (Proven(observation) if denial is None else denial)
