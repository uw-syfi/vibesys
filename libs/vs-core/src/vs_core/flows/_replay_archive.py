"""Validate native replay prefixes independently of the storage implementation."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from protobuf import Oneof

from vs_proto.api import (
    RUN_LOG_MAX_RECORD_BYTES,
    Change,
    EpochStarted,
    Record,
    ReplayStore,
    Span,
    SpanStatus,
    validate,
)
from vs_proto.api import replay as pb

from ._replay_history import AcceptedClock, AcceptedReplay, ReplayAcknowledgements
from ._replay_identity import ENCODING_VERSION
from ._replay_sources import ReplayError

if TYPE_CHECKING:
    from protobuf.wkt.google.protobuf.any_pb import Any


def _execution(span: Span) -> bytes:
    operation = deepcopy(span.replay)
    if operation is None:
        problem = "keyed span requires replay metadata"
        raise ReplayError(problem)
    for name in ("outcome", "accepted_sequence", "paid", "cost_usd"):
        operation.clear_field(name)
    return operation.to_binary()


@dataclass
class ReplayArchive:
    """Disposable validated index over a complete native-record prefix.

    The records, not this index, are authoritative. All accessors return fresh
    protobuf values. Unknown record families and malformed transitions fail
    before reconstruction or dispatch. A logical start without an observation
    requires executor reconciliation; it never proves absence.
    """

    _records: list[bytes] = field(default_factory=list, init=False, repr=False)
    _frames: list[bytes] = field(default_factory=list, init=False, repr=False)
    _spans: dict[str, bytes] = field(default_factory=dict, init=False, repr=False)
    _unfinished: dict[str, None] = field(default_factory=dict, init=False, repr=False)
    _history: list[AcceptedReplay | AcceptedClock] = field(default_factory=list, init=False)
    _epochs: list[bytes] = field(default_factory=list, init=False, repr=False)
    acknowledgements: ReplayAcknowledgements = field(default_factory=ReplayAcknowledgements)
    clock: float = 0.0
    _policy_drift: bytes | None = field(default=None, init=False, repr=False)
    _observed: str | None = field(default=None, init=False, repr=False)
    _epoch_closed: bool = field(default=False, init=False, repr=False)

    @classmethod
    def decode(cls, payload: bytes) -> ReplayArchive:
        store = ReplayStore.from_binary(payload)
        validate(store)
        envelope = deepcopy(store)
        envelope.clear_field("records")
        if envelope.to_binary() != envelope.to_binary(write_unknown_fields=False):
            problem = "replay store contains unknown envelope fields; a newer reader is required"
            raise ReplayError(problem)
        result = cls()
        for record in store.records:
            result.append(record)
        return result

    def encode(self) -> bytes:
        # Repeated-message protobuf fields concatenate. The generated writer
        # frames each Record once; no custom identity or protobuf encoder lives here.
        return b"".join(self._frames)

    def fork(self) -> ReplayArchive:
        """Stage a transition without reparsing or mutating the confirmed prefix."""
        result = ReplayArchive()
        result._records = self._records.copy()
        result._frames = self._frames.copy()
        result._spans = self._spans.copy()
        result._unfinished = self._unfinished.copy()
        result._history = self._history.copy()
        result._epochs = self._epochs.copy()
        result.acknowledgements = self.acknowledgements
        result.clock = self.clock
        result._policy_drift = self._policy_drift
        result._observed = self._observed
        result._epoch_closed = self._epoch_closed
        return result

    @property
    def sequence(self) -> int:
        return len(self._records)

    @property
    def epoch(self) -> int:
        return len(self._epochs)

    @property
    def epoch_closed(self) -> bool:
        """An ended or parked epoch stays closed until a new epoch header opens it."""
        return self._epoch_closed

    @property
    def current_epoch(self) -> EpochStarted:
        if not self._epochs:
            problem = "replay archive has no current epoch"
            raise ReplayError(problem)
        return EpochStarted.from_binary(self._epochs[-1])

    def span(self, span_id: str) -> Span:
        """Return one detached span without decoding the rest of the retained history."""
        value = self._spans.get(span_id)
        if value is None:
            problem = f"unknown replay span {span_id}"
            raise ReplayError(problem)
        return Span.from_binary(value)

    @property
    def history(self) -> tuple[AcceptedReplay | AcceptedClock, ...]:
        return tuple(self._history)

    @property
    def policy_drift(self) -> pb.ReplayPolicyDrift | None:
        return (
            None
            if self._policy_drift is None
            else pb.ReplayPolicyDrift.from_binary(self._policy_drift)
        )

    @property
    def initial_allowance(self) -> pb.ReplayAllowance:
        if not self._epochs:
            problem = "replay archive has no initial epoch"
            raise ReplayError(problem)
        allowance = EpochStarted.from_binary(self._epochs[0]).initial_allowance
        if allowance is None:
            problem = "initial keyed epoch has no historical allowance"
            raise ReplayError(problem)
        return allowance

    @property
    def epochs(self) -> tuple[EpochStarted, ...]:
        return tuple(EpochStarted.from_binary(value) for value in self._epochs)

    @property
    def spans(self) -> tuple[Span, ...]:
        return tuple(Span.from_binary(value) for value in self._spans.values())

    @property
    def unresolved(self) -> tuple[Span, ...]:
        return tuple(self.span(span_id) for span_id in self._unfinished)

    @property
    def pending_observation(self) -> Span | None:
        """The serial observation whose acceptance must precede further clock progress."""
        return None if self._observed is None else Span.from_binary(self._spans[self._observed])

    def append(self, record: Record) -> None:
        """Fold one validated next record; callers discard the index on failure."""
        validate(record)
        envelope = deepcopy(record)
        if envelope.body is not None and envelope.body.field == "epoch_started":
            # Descriptors retain arbitrary annotation extensions so historical
            # strategy messages stay independently decodable. They are content,
            # not executable record metadata.
            envelope.body.value.descriptors = None
        if envelope.to_binary() != envelope.to_binary(write_unknown_fields=False):
            problem = "replay record contains unknown envelope fields; a newer reader is required"
            raise ReplayError(problem)
        if len(record.to_binary()) > RUN_LOG_MAX_RECORD_BYTES:
            problem = "replay record exceeds the native record byte cap; retain content as a blob"
            raise ReplayError(problem)
        if record.seq != self.sequence + 1 or not record.has_field("clock"):
            problem = "replay records require contiguous sequence and Record.clock"
            raise ReplayError(problem)
        if record.clock < self.clock:
            problem = "replay Record.clock must be monotone"
            raise ReplayError(problem)
        if self._observed is not None and (
            record.clock != self.clock or record.replay_clock is not None
        ):
            problem = "accept the pending observation before advancing the replay clock"
            raise ReplayError(problem)
        history_length = len(self._history)
        self._body(record)
        if record.replay_clock is not None:
            if (
                self._epoch_closed
                or self._observed is not None
                or len(self._history) != history_length
            ):
                problem = "one open-epoch policy observation is allowed per record"
                raise ReplayError(problem)
            self._history.append(AcceptedClock.from_record(record))
        self._records.append(record.to_binary())
        self._frames.append(ReplayStore(records=[record]).to_binary())
        self.clock = record.clock

    def _body(self, record: Record) -> None:
        match record.body:
            case Oneof("epoch_started", epoch):
                self._epoch(record, epoch)
            case _ if not self._epochs or record.epoch != self.epoch:
                problem = "replay record belongs to no current epoch"
                raise ReplayError(problem)
            case Oneof("span", body) if body.span is not None:
                if body.span.replay is None:
                    self._plain_span(body.span)
                else:
                    self._span(record, body.change, body.span)
            case Oneof("notice", notice):
                self._notice(notice.detail)
            case Oneof("lifecycle", lifecycle):
                if lifecycle.state is not None and lifecycle.state.field in {"parked", "ended"}:
                    self._epoch_closed = True
            case Oneof("metric" | "stream" | "blob" | "intent" | "chat", _):
                pass
            case _:
                problem = "unsupported record in keyed replay archive"
                raise ReplayError(problem)

    def _notice(self, detail: Any | None) -> None:
        if detail is None:
            return
        name = detail.type_url.rsplit("/", 1)[-1]
        if name == pb.ReplayPolicyDrift.desc().type_name:
            drift = pb.ReplayPolicyDrift.from_binary(detail.value)
            validate(drift)
            if self._policy_drift is not None:
                problem = "an epoch records policy drift only once"
                raise ReplayError(problem)
            self._policy_drift = drift.to_binary()
        elif name == pb.ReplayNotice.desc().type_name:
            notice = pb.ReplayNotice.from_binary(detail.value)
            self.acknowledgements = self.acknowledgements.record_notice(notice)

    def _epoch(self, record: Record, epoch: EpochStarted) -> None:
        if self._epochs and epoch.epoch == len(self._epochs):
            self._amend_epoch(epoch)
            return
        if (
            epoch.epoch != len(self._epochs) + 1
            or epoch.epoch != record.epoch
            or epoch.identity_encoding_version != ENCODING_VERSION
            or epoch.initial_allowance is None
            or epoch.replay_acknowledgement is None
        ):
            problem = (
                "keyed epoch needs sequential identity, encoding, allowance and launch relation"
            )
            raise ReplayError(problem)
        if self._epochs:
            original = self.epochs[0]
            if epoch.run_id != original.run_id:
                problem = "a Resume epoch cannot change the original run identity"
                raise ReplayError(problem)
            if original.run is not None and (
                epoch.run is None
                or epoch.run.strategy != original.run.strategy
                or epoch.run.input != original.run.input
                or epoch.run.objective != original.run.objective
            ):
                problem = "a Resume epoch cannot replace the original task or source input"
                raise ReplayError(problem)
        ack = epoch.replay_acknowledgement
        resumed = self.acknowledgements.resume(ack.launch_request_id)
        if resumed == self.acknowledgements or resumed.acknowledgements[-1] != ack.to_binary():
            problem = "epoch repeats a launch or contradicts the preceding notice acknowledgement"
            raise ReplayError(problem)
        self.acknowledgements = resumed
        self._epochs.append(epoch.to_binary())
        self._policy_drift = None
        self._epoch_closed = False

    def _amend_epoch(self, epoch: EpochStarted) -> None:
        previous = self.current_epoch
        before, after = deepcopy(previous), deepcopy(epoch)
        for name in ("descriptors", "span_kinds", "series"):
            before.clear_field(name)
            after.clear_field(name)
        if before != after:
            problem = "epoch amendment can only grow declarations, not change execution metadata"
            raise ReplayError(problem)
        old_files = [] if previous.descriptors is None else previous.descriptors.file
        new_files = [] if epoch.descriptors is None else epoch.descriptors.file
        groups = (
            (
                [(f.name, f.to_binary()) for f in old_files],
                {f.name: f.to_binary() for f in new_files},
            ),
            (
                [(k.kind, k.to_binary()) for k in previous.span_kinds],
                {k.kind: k.to_binary() for k in epoch.span_kinds},
            ),
            (
                [(s.name, s.to_binary()) for s in previous.series],
                {s.name: s.to_binary() for s in epoch.series},
            ),
        )
        for old, new in groups:
            for name, value in old:
                if new.get(name) != value:
                    problem = f"epoch declaration {name} cannot be replaced or removed"
                    raise ReplayError(problem)
        self._epochs[-1] = epoch.to_binary()

    def _span(self, record: Record, change: Change, span: Span) -> None:
        operation = span.replay
        previous_bytes = self._spans.get(span.id)
        self._check_epoch_open(previous_bytes)
        if operation is None:
            problem = "keyed span requires replay metadata"
            raise ReplayError(problem)
        if previous_bytes is None:
            if change != Change.STARTED or operation.outcome is not None or span.replay_observation:
                problem = "replay operation must start before observation or outcome"
                raise ReplayError(problem)
        else:
            previous = Span.from_binary(previous_bytes)
            if (
                change == Change.STARTED
                or _execution(previous) != _execution(span)
                or previous.started_at != span.started_at
                or previous.parent != span.parent
                or previous.runtime_type != span.runtime_type
            ):
                problem = "replay operation changed immutable start metadata"
                raise ReplayError(problem)
            if previous.has_field("ended_at"):
                self._presentation_update(previous, span, change)
                self._spans[span.id] = span.to_binary()
                return
            old = previous.replay_observation
            if old is not None and (
                span.replay_observation is None
                or old.to_binary() != span.replay_observation.to_binary()
            ):
                problem = "replay observation cannot be replaced or removed"
                raise ReplayError(problem)
        self._observation(span)
        observed = span.replay_observation
        if operation.outcome is not None:
            if (
                previous_bytes is None
                or Span.from_binary(previous_bytes).replay_observation is None
                or observed is None
                or observed.outcome is None
                or operation.outcome.to_binary() != observed.outcome.to_binary()
                or operation.paid != observed.paid
                or operation.cost_usd != observed.cost_usd
                or operation.has_field("cost_usd") != observed.has_field("cost_usd")
                or change != Change.ENDED
            ):
                problem = "acceptance requires its previously durable observation"
                raise ReplayError(problem)
            self._history.append(AcceptedReplay.from_record(record))
            self._observed = None
        elif (change == Change.ENDED or span.has_field("ended_at")) and (
            change != Change.ENDED
            or observed is not None
            or span.status != SpanStatus.STOPPED
            or span.stop is None
            or not span.stop.landed
        ):
            problem = "an unanswered replay start can end only with confirmed withdrawal"
            raise ReplayError(problem)
        self._index_span(span)

    def _check_epoch_open(self, previous_bytes: bytes | None) -> None:
        if self._epoch_closed and (
            previous_bytes is None or not Span.from_binary(previous_bytes).has_field("ended_at")
        ):
            problem = "a closed epoch cannot start or accept replay work"
            raise ReplayError(problem)

    def _index_span(self, span: Span) -> None:
        self._spans[span.id] = span.to_binary()
        if span.has_field("ended_at"):
            self._unfinished.pop(span.id, None)
        else:
            self._unfinished[span.id] = None

    def _observation(self, span: Span) -> None:
        if span.replay is None:
            return
        observed = span.replay_observation
        if observed is not None:
            if self._observed is not None and self._observed != span.id:
                problem = "accept the pending observation before recording another observation"
                raise ReplayError(problem)
            self._observed = span.id
        if observed is not None and (
            observed.outcome is None
            or span.replay.policy is None
            or observed.outcome.type_url.rsplit("/", 1)[-1] != span.replay.policy.reply_type
        ):
            problem = "observation does not match the operation's reply contract"
            raise ReplayError(problem)

    def _plain_span(self, span: Span) -> None:
        previous = self._spans.get(span.id)
        if previous is not None and Span.from_binary(previous).replay is not None:
            problem = "replay execution metadata cannot be removed from a span"
            raise ReplayError(problem)
        self._spans[span.id] = span.to_binary()

    @staticmethod
    def _presentation_update(previous: Span, current: Span, change: Change) -> None:
        if change != Change.UPDATED:
            problem = "accepted replay operation cannot complete twice"
            raise ReplayError(problem)
        if (
            current.replay != previous.replay
            or current.replay_observation != previous.replay_observation
            or current.stop != previous.stop
            or current.status != previous.status
            or current.ended_at != previous.ended_at
        ):
            problem = "presentation update changed accepted execution metadata"
            raise ReplayError(problem)
