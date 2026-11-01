"""The only way runtime code builds a core ``Observation``.

Core accepts a later observation of a request only if it carries a higher
``sequence`` than every earlier one (not necessarily the next one), or is identical to the latest. An executor
that always emits sequence 0 therefore cannot report "Unknown" and later
"Succeeded" for one request: core rejects the second observation.

The factory keeps one durable row per request: the last observation it issued.
Reporting the same facts again returns that row unchanged (a replay, byte for
byte, including the time). Reporting different facts issues the next sequence
and stores it before returning, so a number is never reused. A crash between
issue and delivery (a request begun whose result was never sealed) can leave a
skipped number: the re-run issues the next one. Core takes any observation newer
than the one it holds, so a skipped number loses nothing: an observation is a
sample of the request's state, and the newer sample supersedes it. Identity (event id,
request, scope, admission) is derived from the request and never supplied by
the caller.

Rows live in the shared ``ReceiptStore`` beside the executor's receipts, and the
read-decide-write of one row is atomic across processes. An unreadable row raises ``ObservationLedgerCorruptError`` because a
sequence cannot be chosen without it.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field
from typing import TYPE_CHECKING

from vs_core.api import (
    DecisionId,
    EventId,
    Observation,
    ObservationStatus,
    RequestId,
    ResourceId,
    RevisionRef,
    Scope,
)
from vs_runtime._core_identity import CoreRequestId, core_identity
from vs_runtime._receipt_store import ReceiptCorruptError

if TYPE_CHECKING:
    from vs_core.api import RequestBase
    from vs_runtime._effect_fence import FenceKey, NothingHeld
    from vs_runtime._receipt_store import ReceiptStore


_FAMILY = "observations"
_PART = "observation"


class ObservationLedgerCorruptError(Exception):
    """A stored observation row cannot be read back, so no sequence can be chosen."""


PAYLOAD_CONFLICT = "same request identity with another payload"
"""The diagnostic of every executor's refusal of a reused identity (``payload_conflict``)."""


_VOUCHED = object()
"""Marks facts built by a named constructor that states why the request holds nothing."""


@dataclass(frozen=True)
class ObservationFacts:
    """What an executor learned about one request, without any identity fields.

    Facts that hold nothing (a terminal release that accepts nothing and names no resource)
    end core's owning record, so they come only from a constructor that states why the
    request holds nothing: ``never_held`` (a closed effect fence proves it), ``nothing_held`` (a
    removal), ``refused`` (a self-refusal) or ``not_begun`` (an inspection; core resends the request). Built
    any other way they raise ``TypeError``.
    """

    status: ObservationStatus
    terminal: bool = True
    accepted: bool = False
    released: bool = False
    children: tuple[ResourceId, ...] = ()
    children_complete: bool = False
    resource_id: ResourceId | None = None
    diagnostic: str = ""
    revision: RevisionRef | None = None
    """The revision a snapshot or retain produced; a derived event may carry only this one."""
    measured_input: str | None = None
    """The digest of what an evaluation of ``revision`` reads (``Observation.measured_input``)."""
    _vouch: InitVar[object] = field(default=None, kw_only=True)
    """Init-only, so ``dataclasses.replace`` does not carry it over: a copy that holds nothing raises."""

    def __post_init__(self, _vouch: object) -> None:
        if self.holds_nothing and _vouch is not _VOUCHED:
            message = "facts that hold nothing come only from never_held, nothing_held, refused or not_begun"
            raise TypeError(message)

    @property
    def holds_nothing(self) -> bool:
        """A terminal answer that releases what it names while accepting and naming nothing."""
        return self.terminal and self.released and not self.accepted and self.resource_id is None

    @classmethod
    def payload_conflict(cls) -> ObservationFacts:
        """A terminal refusal of a request identity the executor already holds with another payload.

        The identity's first request may hold a resource, so the refusal proves nothing about
        what the identity holds: it claims no release and no complete children, and core
        learns those only from the first request's own answers.
        """
        return cls(status=ObservationStatus.REJECTED, diagnostic=PAYLOAD_CONFLICT)

    @classmethod
    def never_held(cls, proof: NothingHeld, key: FenceKey, diagnostic: str) -> ObservationFacts:
        """A terminal answer that what the request names holds nothing, now or later.

        Only a closed effect fence proves it (``EffectFences.close`` or ``cleared``): a
        cancel, close or discard of something no request entered the fence to create, or
        whose entries the closer removed. Core ends an owning record only on a proven
        release, so a bare rejection there would leave it waiting for an answer that never
        comes. ``key`` is the fence of what the request names; a proof about another fence,
        or a copy of a proof, raises ``TypeError``.
        """
        proof.vouch(key)
        return cls._holding_nothing(diagnostic)

    @classmethod
    def nothing_held(cls, diagnostic: str) -> ObservationFacts:
        """A terminal answer that what the request names holds nothing now, because it was removed.

        A discard of a generation no ensure bound removes whatever the ensure left unbound, and
        then holds nothing. The step loop's scope order starts no ensure of the generation
        afterwards, so the answer stays true.
        """
        return cls._holding_nothing(diagnostic)

    @classmethod
    def refused(cls, diagnostic: str) -> ObservationFacts:
        """A terminal refusal of a request by itself, decided before it created anything.

        The refusal is sealed with the request's identity, so the request never acts later
        and holds nothing. It is never an answer about what another request created: that
        is ``never_held``, which needs a closed fence.
        """
        return cls._holding_nothing(diagnostic)

    @classmethod
    def not_begun(cls, diagnostic: str) -> ObservationFacts:
        """An inspection's answer that the inspected request's effect has not begun.

        Core sends that request again under the same identity, so whatever it later holds
        is reported under that identity and an earlier delivery cannot act twice.
        """
        return cls._holding_nothing(diagnostic)

    @classmethod
    def _holding_nothing(cls, diagnostic: str) -> ObservationFacts:
        return cls(
            status=ObservationStatus.REJECTED,
            released=True,
            children_complete=True,
            diagnostic=diagnostic,
            _vouch=_VOUCHED,
        )


@dataclass(frozen=True)
class ObservationSubject:
    """Whose observation it is: a request, in the scope and episode core knows it under."""

    request_id: CoreRequestId
    scope: Scope
    admission_id: DecisionId | None

    @classmethod
    def of(
        cls, request: RequestBase, *, request_id: CoreRequestId | None = None
    ) -> ObservationSubject:
        """The request itself, or *request_id* when the request reports on another one.

        An inspection of a target observes the target, in the inspecting
        request's scope and episode, and continues the target's own sequence.
        """
        return cls(request_id or core_identity(request), request.scope, request.admission_id)


class ObservationFactory:
    """Issue observations whose sequence core accepts across retries and restarts."""

    def __init__(self, store: ReceiptStore) -> None:
        self._store = store

    def latest(self, request_id: RequestId) -> Observation | None:
        """The last observation issued for *request_id*, or None when none was."""
        try:
            return self._store.load(_FAMILY, _PART, request_id.root, Observation)
        except ReceiptCorruptError as error:
            message = f"observation row for {request_id.root} is unreadable"
            raise ObservationLedgerCorruptError(message) from error

    def observe(
        self,
        subject: ObservationSubject,
        facts: ObservationFacts,
        *,
        observed_at: float,
        fresh: bool = False,
    ) -> Observation:
        """Return the observation of *subject* reporting *facts*.

        The same facts as the latest issued observation return it unchanged;
        different facts return a new observation with the next sequence.
        ``fresh`` marks a poll of a resource that changes over time: every poll
        is a new observation with the next sequence, even when its facts match
        the last one, because consumers read its time as a new sample.
        """
        key = subject.request_id.root

        def build(latest: Observation | None) -> Observation:
            number = 0 if latest is None else latest.sequence + 1
            return Observation(
                event_id=EventId(root=f"{key}:observation:{number}"),
                request_id=subject.request_id,
                scope=subject.scope,
                admission_id=subject.admission_id,
                sequence=number,
                observed_at=observed_at,
                status=facts.status,
                resource_id=facts.resource_id,
                accepted=facts.accepted,
                terminal=facts.terminal,
                released=facts.released,
                children=facts.children,
                children_complete=facts.children_complete,
                diagnostic=facts.diagnostic,
                revision=facts.revision,
                measured_input=facts.measured_input,
            )

        def decide(latest: Observation | None) -> tuple[Observation | None, Observation]:
            candidate = build(latest)
            if not fresh and latest is not None and _same_facts(latest, candidate):
                return None, latest
            return candidate, candidate

        try:
            return self._store.modify(_FAMILY, _PART, key, Observation, decide)
        except ReceiptCorruptError as error:
            message = f"observation row for {key} is unreadable"
            raise ObservationLedgerCorruptError(message) from error


def _same_facts(stored: Observation, candidate: Observation) -> bool:
    """Equal in everything except the sequence, its event id and the observation time."""
    ignored = {"event_id": stored.event_id, "sequence": stored.sequence}
    return stored.model_copy(update={"observed_at": candidate.observed_at}) == candidate.model_copy(
        update=ignored
    )
