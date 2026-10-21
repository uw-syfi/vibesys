"""Immutable accepted observations and frontier disclosure, with protobuf authority."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from hashlib import sha256
from math import isfinite

from protobuf import Oneof
from protovalidate import validate

from vs_proto.api import Record
from vs_proto.api import replay as pb

from ._replay_identity import ENCODING_VERSION, EncodedIdentity, SemanticKey
from ._replay_sources import ReplayError


@dataclass(frozen=True)
class AcceptedReplay:
    """One logical accepted result, ordered by its enclosing Record.seq/clock."""

    sequence: int
    clock: float
    operation_bytes: bytes

    def __post_init__(self) -> None:
        if self.sequence <= 0 or not isfinite(self.clock) or self.clock < 0:
            problem = "accepted replay needs a positive log sequence and finite nonnegative clock"
            raise ReplayError(problem)
        operation = self.operation()
        validate(operation)
        if operation.outcome is None:
            problem = "accepted replay has no historical outcome"
            raise ReplayError(problem)
        if sha256(operation.identity).digest() != operation.identity_digest:
            problem = "accepted identity digest contradicts retained bytes"
            raise ReplayError(problem)

    @classmethod
    def from_record(cls, record: Record) -> AcceptedReplay:
        validate(record)
        if record.clock is None:
            problem = "keyed replay requires Record.clock"
            raise ReplayError(problem)
        match record.body:
            case Oneof("span", body) if body.span is not None and body.span.replay is not None:
                return cls(record.seq, record.clock, body.span.replay.to_binary())
        problem = "accepted replay requires a span with immutable operation metadata"
        raise ReplayError(problem)

    def operation(self) -> pb.OperationReplay:
        return pb.OperationReplay.from_binary(self.operation_bytes)

    @cached_property
    def key(self) -> SemanticKey:
        operation = self.operation()
        if operation.key is None:
            problem = "accepted operation key is absent"
            raise ReplayError(problem)
        return SemanticKey.from_proto(operation.key)

    @cached_property
    def identity(self) -> EncodedIdentity:
        operation = self.operation()
        return EncodedIdentity(
            operation.identity, operation.dependencies, operation.encoding_version
        )

    @cached_property
    def token(self) -> bytes:
        operation = self.operation()
        # Key and content, not clock/sequence/policy, identify the causal observation.
        return sha256(
            self.key.to_proto().to_binary() + self.identity.digest + _outcome_bytes(operation)
        ).digest()


def _outcome_bytes(operation: pb.OperationReplay) -> bytes:
    if operation.outcome is None:
        problem = "accepted replay has no outcome"
        raise ReplayError(problem)
    return operation.outcome.to_binary()


@dataclass(frozen=True)
class AcceptedClock:
    """One durable timer-only observation, using the Record envelope's clock."""

    sequence: int
    clock: float
    allowance_bytes: bytes

    def __post_init__(self) -> None:
        if self.sequence <= 0 or not isfinite(self.clock) or self.clock < 0:
            problem = "clock observation needs positive sequence and finite nonnegative clock"
            raise ReplayError(problem)
        validate(pb.ReplayAllowance.from_binary(self.allowance_bytes))

    @classmethod
    def from_record(cls, record: Record) -> AcceptedClock:
        validate(record)
        if (
            record.clock is None
            or record.replay_clock is None
            or record.replay_clock.allowance is None
        ):
            problem = "clock observation requires Record.clock and recorded allowance"
            raise ReplayError(problem)
        return cls(record.seq, record.clock, record.replay_clock.allowance.to_binary())

    @property
    def token(self) -> bytes:
        return sha256(b"replay.clock.v1" + float(self.clock).hex().encode()).digest()


@dataclass(frozen=True)
class PolicyDrift:
    """Historical answers are kept; changed roles/templates apply to new work."""

    count: int
    roles: tuple[str, ...]
    templates: tuple[str, ...]

    @property
    def message(self) -> str:
        return "Earlier answers kept; your edits apply to new work."


@dataclass(frozen=True)
class ReplayFrontier:
    """Quiescent reconstruction result. A notice must be durable before dispatch."""

    unused: tuple[AcceptedReplay, ...]
    pending: tuple[SemanticKey, ...]
    drift: PolicyDrift

    @property
    def known_cost_usd(self) -> float:
        return sum(item.operation().cost_usd or 0.0 for item in self.unused)

    @property
    def unknown_cost_count(self) -> int:
        return sum(item.operation().cost_usd is None for item in self.unused)

    @property
    def notice_id(self) -> str | None:
        if not self.unused:
            return None
        return sha256(
            b"unused.v1"
            + b"".join(item.token for item in self.unused)
            + b"".join(sha256(key.to_proto().to_binary()).digest() for key in self.pending)
        ).hexdigest()

    def notice(self) -> pb.ReplayNotice | None:
        notice_id = self.notice_id
        if notice_id is None:
            return None
        return pb.ReplayNotice(
            id=notice_id,
            unused=[item.operation() for item in self.unused],
            pending=[key.to_proto() for key in self.pending],
        )


@dataclass(frozen=True)
class ReplayAcknowledgements:
    """Durable notice/launch relations. Repeated launches cannot acknowledge twice."""

    notices: tuple[bytes, ...] = ()
    acknowledgements: tuple[bytes, ...] = ()

    def record_notice(self, notice: pb.ReplayNotice) -> ReplayAcknowledgements:
        validate(notice)
        known = [pb.ReplayNotice.from_binary(value) for value in self.notices]
        for previous in known:
            if previous.id == notice.id:
                if previous.to_binary() != notice.to_binary():
                    problem = "one replay notice id has contradictory content"
                    raise ReplayError(problem)
                return self
        return ReplayAcknowledgements((*self.notices, notice.to_binary()), self.acknowledgements)

    def resume(self, launch_request_id: str) -> ReplayAcknowledgements:
        if not launch_request_id:
            problem = "Resume requires a launch request id"
            raise ReplayError(problem)
        acks = [pb.ReplayAcknowledgement.from_binary(value) for value in self.acknowledgements]
        if any(ack.launch_request_id == launch_request_id for ack in acks) or not self.notices:
            return self
        notice = pb.ReplayNotice.from_binary(self.notices[-1])
        if any(ack.notice_id == notice.id for ack in acks):
            return self
        ack = pb.ReplayAcknowledgement(notice_id=notice.id, launch_request_id=launch_request_id)
        return ReplayAcknowledgements(self.notices, (*self.acknowledgements, ack.to_binary()))

    def disclosed(self) -> frozenset[bytes]:
        acknowledged = {
            pb.ReplayAcknowledgement.from_binary(value).notice_id for value in self.acknowledgements
        }
        return frozenset(
            _occurrence_token(item)
            for value in self.notices
            for notice in (pb.ReplayNotice.from_binary(value),)
            if notice.id in acknowledged
            for item in notice.unused
        )


def _occurrence_token(operation: pb.OperationReplay) -> bytes:
    if operation.key is None:
        problem = "replay occurrence requires a semantic key"
        raise ReplayError(problem)
    return sha256(operation.key.to_binary() + operation.identity).digest()


def replay_operation(
    key: SemanticKey, identity: EncodedIdentity, policy: pb.ReplayPolicy
) -> pb.OperationReplay:
    """Construct start metadata. The shell installs content and persists before dispatch."""
    return pb.OperationReplay(
        key=key.to_proto(),
        encoding_version=ENCODING_VERSION,
        identity=identity.canonical,
        identity_digest=identity.digest,
        dependencies=identity.dependencies,
        policy=policy,
        allowance=pb.ReplayAllowance(),
    )
