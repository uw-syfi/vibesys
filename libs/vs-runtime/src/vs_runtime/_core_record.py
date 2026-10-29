"""Strict atomic shell record, independent of storage CAS and core revisions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SkipValidation,
    model_validator,
)

from vs_core.api import (
    ContractError,
    Decision,
    OperationRegistry,
    RetainedCandidate,
    RunEnvelope,
    StrategyEvent,
    StrategyState,
    decode_stored,
)
from vs_runtime._core_requests import ExecutionLease, OwnerEvent

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import Comparison, Runaway
    from vs_project.api import StoredEnvelope

RUNTIME_SCHEMA_VERSION = 1


def _integer_schema(value: object) -> object:
    if type(value) is not int:
        message = "schema_version: integer required, no coercion"
        raise ValueError(message)
    return value


PUBLICATION_SCHEMA_VERSION: Literal[1] = 1
type _SchemaVersion = Annotated[Literal[1], BeforeValidator(_integer_schema)]


class Publication(BaseModel):
    """Stable semantic publication identity, preserved across append/ack crashes."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    publication_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    event: StrategyEvent


class RuntimeRecord[S: StrategyState](BaseModel):
    """One StateStore payload; publication acknowledgements do not advance core.

    pending_inputs holds executor owner events committed in the same write as
    their observation. Each leaves this tuple in the commit that applies it, so
    a crash between the observation and its owner events loses none of them.
    pending_decisions and wakes persist what the search asked for, in the commit that
    handled the event that made it ask, so a crash loses neither.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: _SchemaVersion
    envelope: RunEnvelope[S]
    pending_publications: tuple[Publication, ...]
    delivery_cursor: int = Field(ge=0)
    next_publication_sequence: int = Field(ge=1)
    pending_inputs: tuple[OwnerEvent, ...]
    pending_decisions: tuple[Decision, ...] = ()
    """Decisions the search made that core could not take yet (recovery was not ready)."""
    wakes: tuple[float, ...] = ()
    """Run-clock times the search asked to be woken at, earliest first, until they are reached."""

    @model_validator(mode="after")
    def publication_order(self) -> RuntimeRecord[S]:
        """Reject gaps, duplicates and unacknowledged sequence loss."""
        self.check_publications()
        return self

    def check_publications(self) -> None:
        """The publication invariants, for records built by ``model_copy`` (no validators)."""
        if self.next_publication_sequence != self.envelope.event_cursor.sequence + 1:
            message = "next_publication_sequence: must follow the envelope event_cursor"
            raise ValueError(message)
        if len(
            self.pending_publications
        ) != self.next_publication_sequence - self.delivery_cursor - 1 or any(
            row.sequence != sequence
            for sequence, row in enumerate(
                self.pending_publications, start=self.delivery_cursor + 1
            )
        ):
            message = "pending_publications: sequences must cover delivery_cursor to next_publication_sequence"
            raise ValueError(message)
        run_id = self.envelope.core.run.run_id.root
        if any(
            row.publication_id != f"{run_id}:{row.sequence}" for row in self.pending_publications
        ):
            message = "pending_publications: publication_id must match run and sequence"
            raise ValueError(message)

    @classmethod
    def fresh(cls, envelope: RunEnvelope[S]) -> Self:
        """Construct the first complete record, without implicit wire defaults."""
        return cls(
            schema_version=RUNTIME_SCHEMA_VERSION,
            envelope=envelope,
            pending_publications=(),
            delivery_cursor=0,
            next_publication_sequence=1,
            pending_inputs=(),
            pending_decisions=(),
            wakes=(),
        )

    @classmethod
    def decode(cls, stored: StoredEnvelope, registry: OperationRegistry) -> Self:
        """Validate the runtime schema and canonical core codecs before replay."""
        if stored.schema_version != RUNTIME_SCHEMA_VERSION:
            raise ContractError(("runtime", "schema_version"), "unsupported record schema")
        record = decode_stored(
            cls,
            stored.payload,
            ("envelope", "schema_version"),
            context={"operation_registry": registry, "persisted_operation": True},
        )
        envelope = registry.decode_envelope(
            type(record.envelope), record.envelope.model_dump_json()
        )
        return record.model_copy(update={"envelope": envelope})


class PublicationLease(ExecutionLease, Protocol):
    """The host's lease, as a publication delivery uses it to write."""

    def write_if_held(self, mutation: Callable[[], None]) -> bool:
        """Run *mutation* in the store transaction that checks this host still owns the run.

        Ownership is judged at the latest time the host has seen, with its current fence,
        not at the time the publication started: a renewal while a delivery awaits moves
        the store's time watermark past that start, and the host still owns the run.
        Returns False, without running *mutation*, when the host no longer does.
        """
        ...


class PublicationContext(BaseModel):
    """Publication execution epoch, separate from its stable identity."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, arbitrary_types_allowed=True
    )
    now_at: float = Field(ge=0, allow_inf_nan=False)
    """When the publication started; ownership at the write is the lease's to judge."""
    lease: SkipValidation[PublicationLease] = Field(exclude=True, repr=False)


class PublicationAcknowledgement(BaseModel):
    """Positive durable acknowledgement, bound to the exact publication."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    publication_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)


class PublicationHistory(BaseModel):
    """One authoritative strict journal wire contract for all implementations."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: _SchemaVersion
    publications: tuple[Publication, ...]

    @model_validator(mode="after")
    def ordered_identities(self) -> Self:
        """Reject duplicate IDs, gaps and mixed-run histories before append."""
        run_id = (
            self.publications[0].publication_id.rsplit(":", 1)[0] if self.publications else None
        )
        for sequence, publication in enumerate(self.publications, start=1):
            if (
                publication.sequence != sequence
                or publication.publication_id != f"{run_id}:{sequence}"
            ):
                message = "publications: contiguous stable identities from one run required"
                raise ValueError(message)
        return self


def append_publication(
    rows: tuple[Publication, ...], publication: Publication
) -> tuple[Publication, ...]:
    """Exact replay is idempotent; conflicting identities or gaps fail loudly."""
    prior = next((row for row in rows if row.publication_id == publication.publication_id), None)
    if prior is not None:
        if prior != publication:
            raise ContractError(("publication_id",), "publication payload conflict")
        return rows
    if publication.sequence != len(rows) + 1:
        raise ContractError(("sequence",), "publication sequence gap or conflict")
    return PublicationHistory(schema_version=1, publications=(*rows, publication)).publications


class WinnerSelection(BaseModel):
    """The run result core's settlement state has chosen, as plain data for a frontend.

    ``selection`` says whether a retained candidate or the unchanged trusted input was
    chosen; ``revision`` is the commit (or revision id) that becomes the result and
    ``settlement_id`` names the settled attempt behind a candidate.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    selection: Literal["candidate", "baseline"]
    revision: str = Field(min_length=1)
    settlement_id: str | None = None


def new_comparisons(
    previous: RuntimeRecord[Any] | None, current: RuntimeRecord[Any]
) -> tuple[Comparison, ...]:
    """The comparisons of candidates with their bases that ``current`` logged beyond ``previous``.

    Core's log of comparisons only grows, so the new ones are the tail past what ``previous``
    held; with no previous record, all of them. Every strategy measures through core, so this
    is the one place that sees all of them.
    """
    held = 0 if previous is None else len(previous.envelope.core.evaluation.comparisons)
    return current.envelope.core.evaluation.comparisons[held:]


def new_runaways(
    previous: RuntimeRecord[Any] | None, current: RuntimeRecord[Any]
) -> tuple[Runaway, ...]:
    """The runaway evaluation counts that ``current`` recorded beyond ``previous``.

    Like the comparisons, core's log of them only grows, so the new ones are the tail.
    """
    held = 0 if previous is None else len(previous.envelope.core.evaluation.runaways)
    return current.envelope.core.evaluation.runaways[held:]


def winner_selection(record: RuntimeRecord[Any] | None) -> WinnerSelection | None:
    """The selection a committed record holds, or None before any strategy proposed a winner.

    Every strategy proposes through core, so this is the one place that sees all of them.
    """
    if record is None:
        return None
    adoption = record.envelope.core.settlement.adoption
    if adoption is None:
        return None
    chosen = adoption.selection
    revision = chosen.revision.git_commit or chosen.revision.revision_id.root
    if isinstance(chosen, RetainedCandidate):
        return WinnerSelection(
            selection="candidate", revision=revision, settlement_id=chosen.settlement_id.root
        )
    return WinnerSelection(selection="baseline", revision=revision)
