"""Pure physical-child invariants; logical identity never contains an execution ID."""

from __future__ import annotations

import hashlib
from copy import deepcopy

from vs_proto.api import Change, Span, SpanStatus
from vs_proto.api import replay as pb

from ._replay_sources import ReplayError
from ._replay_turns import effective_deadline


def physical_start(
    parent: Span, *, identity: str, host: str, deadline: float, ordinal: int
) -> Span:
    child = deepcopy(parent)
    child.id, child.parent = identity, parent.id
    child.replay_physical = pb.ReplayPhysical(
        logical=parent.id,
        external_id=identity,
        host_incarnation=host,
        deadline=deadline,
        ordinal=ordinal,
    )
    return child


def execution_metadata(span: Span) -> bytes:
    operation = deepcopy(span.replay)
    if operation is None:
        problem = "keyed span requires replay metadata"
        raise ReplayError(problem)
    for name in ("outcome", "accepted_sequence", "paid", "cost_usd", "accepted_child"):
        operation.clear_field(name)
    return operation.to_binary()


def _physical_metadata(span: Span) -> bytes:
    physical = deepcopy(span.replay_physical)
    if physical is None:
        problem = "physical child metadata is missing"
        raise ReplayError(problem)
    for name in (
        "withdrawn_sequence",
        "observed_sequence",
        "termination_proof_digest",
        "confirmed_absent",
        "settlement_sequence",
    ):
        physical.clear_field(name)
    return physical.to_binary()


def validate_physical(
    previous: Span | None, span: Span, parent: Span, *, sequence: int, change: Change
) -> None:
    physical, operation = span.replay_physical, span.replay
    if physical is None or operation is None:
        problem = "physical child requires immutable execution metadata"
        raise ReplayError(problem)
    if (
        physical.logical != parent.id
        or span.parent != parent.id
        or physical.external_id != span.id
        or parent.replay_physical is not None
        or execution_metadata(span) != execution_metadata(parent)
        or operation.outcome is not None
        or operation.has_field("accepted_child")
    ):
        problem = "physical child changed its logical parent or external identity"
        raise ReplayError(problem)
    if previous is None:
        _validate_start(span, parent, change)
    else:
        _validate_update(previous, span, change)
    _validate_observation(previous, span, sequence)
    _validate_intents(previous, span, sequence)
    if (
        parent.replay is not None
        and parent.replay.turn_bound is not None
        and physical.deadline != effective_deadline(parent.replay.turn_bound)
    ):
        problem = "physical deadline differs from its immutable credited turn bound"
        raise ReplayError(problem)
    if span.has_field("ended_at"):
        observed = span.replay_observation is not None
        withdrawn = physical.has_field("withdrawn_sequence")
        stopped = (
            span.status is SpanStatus.STOPPED
            and span.stop is not None
            and span.stop.landed
            and (physical.confirmed_absent or physical.has_field("termination_proof_digest"))
        )
        if not (
            (observed and not withdrawn and span.status is SpanStatus.DONE)
            or (withdrawn and stopped)
        ):
            problem = "a physical child ends only with a result or confirmed termination"
            raise ReplayError(problem)


def _validate_intents(previous: Span | None, span: Span, sequence: int) -> None:
    physical = span.replay_physical
    if physical is None:
        return
    old = None if previous is None else previous.replay_physical
    for name in ("withdrawn_sequence", "settlement_sequence"):
        if (
            physical.has_field(name)
            and (old is None or not old.has_field(name))
            and getattr(physical, name) != sequence
        ):
            problem = f"{name} must come from its durable record"
            raise ReplayError(problem)
    if physical.has_field("settlement_sequence") and not (
        physical.has_field("withdrawn_sequence") and physical.has_field("termination_proof_digest")
    ):
        problem = "settlement intent requires withdrawal and committed environment proof"
        raise ReplayError(problem)


def _validate_start(span: Span, parent: Span, change: Change) -> None:
    physical = span.replay_physical
    if (
        physical is None
        or change is not Change.STARTED
        or parent.has_field("ended_at")
        or span.replay_observation is not None
        or span.has_field("ended_at")
        or physical.has_field("withdrawn_sequence")
        or physical.has_field("observed_sequence")
        or physical.has_field("termination_proof_digest")
        or physical.confirmed_absent
        or physical.has_field("settlement_sequence")
    ):
        problem = "physical execution must start before observation, withdrawal or termination"
        raise ReplayError(problem)


def _validate_update(previous: Span, span: Span, change: Change) -> None:
    if (
        change is Change.STARTED
        or _physical_metadata(previous) != _physical_metadata(span)
        or previous.started_at != span.started_at
        or previous.runtime_type != span.runtime_type
    ):
        problem = "physical execution changed immutable start metadata"
        raise ReplayError(problem)
    old, current = previous.replay_physical, span.replay_physical
    if old is None or current is None:
        problem = "physical execution metadata cannot be removed"
        raise ReplayError(problem)
    for name in (
        "withdrawn_sequence",
        "observed_sequence",
        "termination_proof_digest",
        "settlement_sequence",
    ):
        if old.has_field(name) and (
            not current.has_field(name) or getattr(old, name) != getattr(current, name)
        ):
            problem = f"physical {name} cannot be replaced or removed"
            raise ReplayError(problem)
    if old.confirmed_absent and not current.confirmed_absent:
        problem = "confirmed absence cannot be withdrawn"
        raise ReplayError(problem)
    if previous.has_field("ended_at") and (
        previous.status != span.status
        or previous.ended_at != span.ended_at
        or previous.stop != span.stop
    ):
        problem = "physical termination cannot be changed"
        raise ReplayError(problem)


def _validate_observation(previous: Span | None, span: Span, sequence: int) -> None:
    physical, observed = span.replay_physical, span.replay_observation
    if physical is None:
        return
    old = None if previous is None else previous.replay_observation
    if observed is not None and (
        observed.outcome is None
        or span.replay is None
        or span.replay.policy is None
        or observed.outcome.type_url.rsplit("/", 1)[-1] != span.replay.policy.reply_type
    ):
        problem = "physical observation differs from its retained reply contract"
        raise ReplayError(problem)
    if old is not None and old != observed:
        problem = "physical observation cannot be replaced or removed"
        raise ReplayError(problem)
    if (observed is not None) != physical.has_field("observed_sequence"):
        problem = "physical observations require their durable source sequence"
        raise ReplayError(problem)
    if physical.confirmed_absent and observed is not None:
        problem = "a positively absent invocation cannot have execution observations"
        raise ReplayError(problem)
    if observed is not None and old is None and physical.observed_sequence != sequence:
        problem = "physical observation sequence must come from its durable record"
        raise ReplayError(problem)


def proof_covers(child: Span, proof: pb.WriterTerminationProof, *, run_id: str) -> None:
    physical = child.replay_physical
    manifest = proof.manifest
    if (
        physical is None
        or manifest is None
        or not any(
            writer.run_id == run_id
            and writer.host_incarnation == physical.host_incarnation
            and writer.physical_id == child.id
            for writer in manifest.writers
        )
    ):
        problem = "Recovery blocked: previous writers may still modify this workspace; environment proof does not cover this physical writer and host incarnation"
        raise ReplayError(problem)


def proof_digest(proof: pb.WriterTerminationProof) -> bytes:
    return hashlib.sha256(proof.to_binary()).digest()


def physical_identity(span: Span) -> bytes:
    """Immutable dispatch identity, shared by real owners and their in-memory Fake."""
    operation = execution_metadata(span)
    return operation if span.replay_physical is None else operation + _physical_metadata(span)
