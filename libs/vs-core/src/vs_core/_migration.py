"""Selected pure migration of frozen version-2 envelopes, never implicit decode."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import TYPE_CHECKING

from ._registry import ContractError, EnvelopeMigration, OperationRegistry
from ._values import canonical_json
from .types.attempts import AttemptBudget
from .types.common import AttemptId, ExecuteRegisteredOperation, LifecycleClass
from .types.evaluation import SubmitMeasurement
from .types.intents import IntentPhase, RecoveryBarrier, RecoveryPhase
from .types.kernel import CoreState, DecisionReceipt
from .types.sessions import ResumeSessionTurn
from .types.strategy import Accepted, Operation

_SOURCE_VERSION = 2
_TARGET_VERSION = 3

if TYPE_CHECKING:
    from .types.common import Value


def _object(value: object, path: tuple[str | int, ...]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ContractError(path, "object required for version-2 migration")
    return value


def _rows(value: object, path: tuple[str | int, ...]) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ContractError(path, "array required for version-2 migration")
    return [_object(row, (*path, index)) for index, row in enumerate(value)]


def _absent_fields(
    value: dict[str, object], fields: tuple[str, ...], path: tuple[str | int, ...]
) -> None:
    for field in fields:
        if field in value:
            raise ContractError((*path, field), "unknown field in version-2 source")


def _unverified_evidence(rows: object, path: tuple[str | int, ...]) -> None:
    """Upgrade only declared EvidenceRef paths, never registered payload values."""
    for index, evidence in enumerate(_rows(rows, path)):
        _absent_fields(evidence, ("acceptance_receipt",), (*path, index))
        evidence["acceptance_receipt"] = None


def _new_evaluation_contracts(core: dict[str, object]) -> None:
    evaluation = _object(core.get("evaluation"), ("core", "evaluation"))
    _unverified_evidence(evaluation.get("evidence"), ("core", "evaluation", "evidence"))
    for group in ("jobs", "registered_jobs"):
        for index, job in enumerate(_rows(evaluation.get(group), ("core", "evaluation", group))):
            _absent_fields(job, ("expected_measurement",), ("core", "evaluation", group, index))
            _unverified_evidence(
                job.get("evidence"), ("core", "evaluation", group, index, "evidence")
            )
    continuations = _rows(evaluation.get("continuations"), ("core", "evaluation", "continuations"))
    intents = _object(core.get("intents"), ("core", "intents"))
    continuations.extend(
        _object(row["suspension"], ("suspension",))
        for row in _rows(intents.get("intents"), ("core", "intents", "intents"))
        if row.get("suspension") is not None
    )
    for continuation in continuations:
        _absent_fields(
            continuation, ("authorization_receipt", "preceding_submission"), ("continuation",)
        )
        # Version 2 did not record the suspension's preceding submission. An
        # unknown position cannot establish the beginning of certified history.
        continuation["preceding_submission"] = None
        _unverified_evidence(continuation.get("evidence"), ("continuation", "evidence"))


def _new_attempt_contracts(core: dict[str, object]) -> None:
    attempts = _object(core.get("attempts"), ("core", "attempts"))
    for index, attempt in enumerate(
        _rows(attempts.get("attempts"), ("core", "attempts", "attempts"))
    ):
        budget = _object(attempt.get("budget"), ("core", "attempts", "attempts", index, "budget"))
        _absent_fields(
            budget, ("repeated_failure_limit",), ("core", "attempts", "attempts", index, "budget")
        )
        _absent_fields(
            attempt,
            ("evaluation_history", "terminal_reason"),
            ("core", "attempts", "attempts", index),
        )
        budget["repeated_failure_limit"] = AttemptBudget().repeated_failure_limit
        attempt["evaluation_history"] = {
            "availability": "unavailable",
            "covered_submissions": [],
            "records": [],
        }
        attempt["terminal_reason"] = None


def _new_child_contracts(core: dict[str, object]) -> None:
    intents = _object(core.get("intents"), ("core", "intents"))
    for child in _rows(intents.get("children"), ("core", "intents", "children")):
        _absent_fields(
            child,
            ("observation_watermarks", "watermark_history_complete"),
            ("core", "intents", "children"),
        )
        # Keep the aggregate observation as its source's sequence lower bound.
        # Only a fresh correlated inspection creates a certified watermark.
        child["observation_watermarks"] = []
        # Version 2 discarded other sources' earlier maxima. The one retained
        # observation cannot establish completeness, even for a one-source child.
        child["watermark_history_complete"] = False


def _registered_input_manifests(core: dict[str, object]) -> None:
    """Reconstruct transport only from an exact durable invocation manifest."""
    sessions = _object(core.get("sessions"), ("core", "sessions"))
    invocations = _rows(sessions.get("invocations"), ("core", "sessions", "invocations"))
    inputs = _rows(sessions.get("inputs"), ("core", "sessions", "inputs"))
    for index, invocation in enumerate(invocations):
        _absent_fields(
            invocation,
            ("evaluation_prefix", "pending_suspension"),
            ("core", "sessions", "invocations", index),
        )
        # Version 2 retained no invocation-owned pending yield. Neither an
        # evaluation continuation nor a sibling intent can reconstruct it.
        invocation["pending_suspension"] = None
    intents = _object(core.get("intents"), ("core", "intents"))
    for index, intent in enumerate(_rows(intents.get("intents"), ("core", "intents", "intents"))):
        _absent_fields(
            intent, ("setup_failure", "evaluation_result"), ("core", "intents", "intents", index)
        )
        request = _object(intent.get("request"), ("core", "intents", "intents", index, "request"))
        if request.get("kind") != "execute_registered_operation":
            continue
        _absent_fields(request, ("inputs",), ("core", "intents", "intents", index, "request"))
        request["inputs"] = []
        matching = [
            row
            for row in invocations
            if row.get("registered_operation") == request.get("operation_id")
        ]
        if len(matching) > 1:
            raise ContractError(
                ("core", "intents", "intents", index, "inputs"), "ambiguous invocation manifest"
            )
        if not matching:
            operation = _object(request.get("operation"), ("operation",))
            schema = _object(operation.get("schema_ref"), ("operation", "schema_ref"))
            if schema.get("lifecycle") == LifecycleClass.SESSION_TURN and any(
                row.get("reserved_to") is not None for row in inputs
            ):
                raise ContractError(
                    ("core", "intents", "intents", index, "inputs"),
                    "missing invocation correspondence",
                )
            continue
        invocation = matching[0]
        if invocation.get("scope") != request.get("scope"):
            raise ContractError(
                ("core", "intents", "intents", index, "inputs"),
                "invocation scope correspondence mismatch",
            )
        request["inputs"] = _input_occurrences(
            invocation, inputs, ("core", "intents", "intents", index, "inputs")
        )


def _input_occurrences(
    invocation: dict[str, object], inputs: list[dict[str, object]], path: tuple[str | int, ...]
) -> list[dict[str, object]]:
    input_ids = _rows(invocation.get("input_ids", []), (*path, "input_ids"))
    artifacts = _rows(invocation.get("reserved_inputs", []), (*path, "reserved_inputs"))
    if len(input_ids) != len(artifacts):
        raise ContractError(path, "artifact history lacks exact occurrence correspondence")
    reserved = [row for row in inputs if row.get("reserved_to") == invocation.get("invocation")]
    if len(reserved) != len(input_ids):
        raise ContractError(path, "unexplained reserved occurrence correspondence")
    occurrences: list[dict[str, object]] = []
    for input_id, artifact in zip(input_ids, artifacts, strict=True):
        records = [
            row
            for row in reserved
            if _object(row.get("input"), (*path, "input")).get("input_id") == input_id
        ]
        if len(records) != 1:
            raise ContractError(path, "missing exact reserved occurrence")
        payload = _object(records[0]["input"], (*path, "input"))
        if payload.get("artifact") != artifact:
            raise ContractError(path, "reserved artifact differs from invocation manifest")
        occurrences.append({"input_id": input_id, "artifact": artifact})
    return occurrences


def _validate_old_decisions(core: dict[str, object]) -> None:
    run = _object(core.get("run"), ("core", "run"))
    for index, receipt in enumerate(_rows(run.get("receipts"), ("core", "run", "receipts"))):
        if receipt.get("decision") is None:
            continue
        decision = _object(receipt["decision"], ("core", "run", "receipts", index, "decision"))
        if decision.get("kind") == "start_attempt":
            budget = _object(
                decision.get("budget"), ("core", "run", "receipts", index, "decision", "budget")
            )
            _absent_fields(
                budget,
                ("repeated_failure_limit",),
                ("core", "run", "receipts", index, "decision", "budget"),
            )
        if decision.get("kind") == "operation":
            _absent_fields(
                decision,
                ("normalized_measurement",),
                ("core", "run", "receipts", index, "decision"),
            )


def _validate_prepared_authority(state: CoreState, registry: OperationRegistry) -> None:
    """New bounds cannot bless already-prepared work from an unknown prefix."""
    for index, intent in enumerate(state.intents.intents):
        if intent.phase != IntentPhase.PREPARED:
            continue
        request = intent.request
        if isinstance(request, ResumeSessionTurn):
            raise ContractError(
                ("core", "intents", "intents", index, "request"),
                "resume publication/history proof unavailable in version 2",
            )
        if isinstance(request, SubmitMeasurement) and isinstance(request.scope.owner, AttemptId):
            raise ContractError(
                ("core", "intents", "intents", index, "request"),
                "attempt evaluation history unavailable in version 2",
            )
        if not isinstance(request, ExecuteRegisteredOperation):
            continue
        decoded = registry.decode(request.operation)
        if (
            isinstance(request.scope.owner, AttemptId)
            and registry.normalize_measurement(decoded) is not None
        ):
            raise ContractError(
                ("core", "intents", "intents", index, "request"),
                "registered evaluation history unavailable in version 2",
            )
        turn = registry.normalize_turn(decoded)
        if turn is not None and turn.continuation_id is not None:
            raise ContractError(
                ("core", "intents", "intents", index, "request"),
                "registered resume publication/history proof unavailable in version 2",
            )


def _validate_registered_turn_correspondence(state: CoreState) -> None:
    """An operation ID alone cannot prove which wire or invocation was reserved."""
    for index, intent in enumerate(state.intents.intents):
        request = intent.request
        if (
            not isinstance(request, ExecuteRegisteredOperation)
            or request.operation.schema_ref.lifecycle != LifecycleClass.SESSION_TURN
        ):
            continue
        origins = [
            receipt
            for receipt in state.run.receipts
            if isinstance(receipt.decision, Operation)
            and request.operation_id.root == f"operation:{receipt.decision_id.root}"
        ]
        if len(origins) != 1:
            raise ContractError(
                ("core", "intents", "intents", index, "request"),
                "missing canonical registered turn receipt",
            )
        receipt = origins[0]
        decision = receipt.decision
        if not isinstance(decision, Operation):
            raise ContractError(
                ("core", "intents", "intents", index, "request"),
                "missing registered operation decision",
            )
        _validate_registered_turn_origin(state, request, receipt, decision, index)


def _validate_registered_turn_origin(
    state: CoreState,
    request: ExecuteRegisteredOperation,
    receipt: DecisionReceipt,
    decision: Operation,
    index: int,
) -> None:
    schema = request.operation.schema_ref
    descriptor = next((row for row in state.registry if row.kind == schema.kind), None)
    offered = next(
        (row for row in state.run.capabilities.operations if row.kind == schema.kind), None
    )
    if (
        descriptor is None
        or descriptor != offered
        or descriptor.request_schema != schema.request_schema
        or descriptor.outcome_schema != schema.outcome_schema
        or descriptor.lifecycle != schema.lifecycle
    ):
        raise ContractError(
            ("core", "intents", "intents", index, "request"),
            "registered turn lacks exact offered descriptor authority",
        )
    if (
        not isinstance(receipt.feedback, Accepted)
        or receipt.feedback.decision_id != receipt.decision_id
        or decision.decision_id != receipt.decision_id
        or request.decision_id != receipt.decision_id
        or request.request_id not in receipt.request_ids
    ):
        raise ContractError(
            ("core", "intents", "intents", index, "request"), "unaccepted registered turn receipt"
        )
    if (
        decision.scope != request.scope
        or decision.deadline_at != request.deadline_at
        or decision.registered_wire != request.operation
    ):
        raise ContractError(
            ("core", "intents", "intents", index, "request"),
            "registered turn payload/scope/deadline correspondence mismatch",
        )
    invocations = [
        invocation
        for invocation in state.sessions.invocations
        if invocation.registered_operation == request.operation_id
    ]
    if len(invocations) != 1:
        raise ContractError(
            ("core", "intents", "intents", index, "request"), "missing exact registered invocation"
        )
    invocation = invocations[0]
    turn = decision.registered_turn
    if (
        turn is None
        or invocation.turn != turn
        or invocation.scope != request.scope
        or invocation.invocation.session_id != turn.session.session_id
        or invocation.invocation.invocation_id != turn.invocation_id
        or invocation.invocation.generation != request.scope.generation
    ):
        raise ContractError(
            ("core", "intents", "intents", index, "request"),
            "registered invocation correspondence mismatch",
        )


def _digest(value: Value) -> str:
    return sha256(canonical_json(value).encode()).hexdigest()


def v2_to_v3_migration(registry: OperationRegistry) -> EnvelopeMigration:
    """Choose migration with the exact codecs needed for recorded operations.

    Missing history, source acceptance and publication receipts remain explicitly
    unavailable. Existing IDs, observations, accounting and deadlines survive.
    Canonical envelope digests are recomputed for expanded typed contracts;
    registered operation wire payloads and schema identities remain unchanged.
    Missing input occurrence correspondence or prepared continuation/bounded
    measurement authority rejects rather than inventing a dispatchable payload.
    Every migrated envelope requires fresh recovery before ordinary dispatch;
    original run status and recovery epoch are preserved. The ordinary decoder
    never selects this conversion.
    """

    def rewrite(source: str) -> str:
        envelope = _object(json.loads(source), ())
        if (
            type(envelope.get("schema_version")) is not int
            or envelope["schema_version"] != _SOURCE_VERSION
        ):
            raise ContractError(("schema_version",), "version 2 required")
        core = _object(envelope.get("core"), ("core",))
        _validate_old_decisions(core)
        _new_attempt_contracts(core)
        _new_child_contracts(core)
        _registered_input_manifests(core)
        _new_evaluation_contracts(core)
        sessions = _object(core.get("sessions"), ("core", "sessions"))
        _absent_fields(sessions, ("run_checkpoints",), ("core", "sessions"))
        run = _object(core.get("run"), ("core", "run"))
        limits = _object(run.get("limits"), ("core", "run", "limits"))
        _absent_fields(limits, ("pool_capacities",), ("core", "run", "limits"))
        # Parsing through the new authoritative contracts supplies only defaults
        # whose absence denies authority. It also rejects unknown historical keys.
        state = CoreState.model_validate_json(
            json.dumps(core), context={"operation_registry": registry}
        )
        _validate_prepared_authority(state, registry)
        _validate_registered_turn_correspondence(state)
        state = state.model_copy(
            update={
                "intents": state.intents.model_copy(
                    update={
                        "recovery": RecoveryBarrier(
                            epoch=state.intents.recovery.epoch, phase=RecoveryPhase.REQUIRED
                        ),
                        "intents": tuple(
                            intent.model_copy(update={"payload_digest": _digest(intent.request)})
                            for intent in state.intents.intents
                        ),
                    }
                ),
                "run": state.run.model_copy(
                    update={
                        "receipts": tuple(
                            receipt.model_copy(update={"payload_digest": _digest(receipt.decision)})
                            if receipt.decision is not None
                            else receipt
                            for receipt in state.run.receipts
                        ),
                    }
                ),
            }
        )
        envelope["core"] = state.model_dump(mode="json")
        envelope["schema_version"] = _TARGET_VERSION
        return json.dumps(envelope, sort_keys=True, separators=(",", ":"))

    return EnvelopeMigration(
        source_version=_SOURCE_VERSION, target_version=_TARGET_VERSION, rewrite=rewrite
    )
