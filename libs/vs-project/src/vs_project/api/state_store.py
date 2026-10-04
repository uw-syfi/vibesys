"""Atomic opaque run records and host leases, independent of kernel schemas.

The shell serializes the complete kernel envelope, including state, request
outbox and cursor, into ``payload``. Metadata permits CAS without decoding it.
Time is finite nonnegative seconds on one durable clock basis shared by hosts.
Fencing commits does not fence external executors: they must honor the epoch
or reconcile stable request identities before replay.
"""

from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class _Value(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, ser_json_bytes="base64", val_json_bytes="base64"
    )


class StoredEnvelope(_Value):
    """Opaque complete envelope with an independent storage CAS revision.

    Storage revision starts at zero and increments for every replacement,
    including quarantine. It is not the kernel revision inside opaque payload.
    """

    kind: Literal["envelope"] = "envelope"
    revision: int = Field(ge=0)
    schema_version: int = Field(ge=1)
    payload: bytes


class QuarantinedEnvelope(_Value):
    """Blocked migration replacing the active record, with no dispatch authority.

    Payload preserves the unmigratable source bytes; reason preserves its
    diagnostic. None schema_version records missing or unparseable source
    metadata without inventing a version. This record is an alternative to a
    runnable envelope.
    """

    kind: Literal["quarantine"] = "quarantine"
    revision: int = Field(ge=0)
    schema_version: int | None = Field(ge=1)
    payload: bytes
    reason: str = Field(min_length=1, pattern=r"\S")


StoreRecord = Annotated[StoredEnvelope | QuarantinedEnvelope, Field(discriminator="kind")]


class StoreFence(_Value):
    """Storage lease identity, mapped to the kernel host fence by the shell.

    Epoch and host identify ownership. The persisted expiry is authoritative,
    so a token retained before same-epoch renewal remains valid.
    """

    host_id: str = Field(min_length=1, pattern=r"^\S+$")
    epoch: int = Field(ge=1)
    expires_at: float = Field(ge=0, allow_inf_nan=False)


class ConflictReason(StrEnum):
    """Reasons a CAS was rejected without publication."""

    REVISION = "revision"
    FENCE = "fence"


class Committed(_Value):
    """The complete candidate was durably published under a valid fence."""

    kind: Literal["committed"] = "committed"
    record: StoreRecord


class Conflict(_Value):
    """Nothing was written; revision or host ownership no longer matches."""

    kind: Literal["conflict"] = "conflict"
    reason: ConflictReason
    revision: int | None = Field(ge=0)


class Unknown(_Value):
    """Publication is uncertain. Dispatch is forbidden until reload resolves it.

    Reload sees the complete old or new record. Compare the attempted revision
    and payload; if another host advanced it, reconcile instead of replaying.
    """

    kind: Literal["unknown"] = "unknown"
    revision: int = Field(ge=0)


CommitOutcome = Annotated[Committed | Conflict | Unknown, Field(discriminator="kind")]


class CommitFault(StrEnum):
    """Deterministic fault plan, consumed only by eligible record mutations."""

    FAILED = "failed"
    UNKNOWN_BEFORE = "unknown_before"
    UNKNOWN_AFTER = "unknown_after"


class StateStoreWriteError(OSError):
    """An injected definite failure published nothing; dispatch is forbidden."""


class StateStore(Protocol):
    """Atomic record CAS and epoch fencing with interchangeable implementations.

    None expected revision means no record. The initial candidate is revision
    zero; every subsequent mutation is exactly the next revision. Invalid
    inputs raise ValueError. I/O observation errors propagate; write errors
    return Unknown because replacement may already have happened. Lease-write
    errors propagate and grant no dispatch authority until reconciliation.
    """

    def load(self) -> StoreRecord | None:
        """Load the latest whole record, including a blocked quarantine."""
        ...

    def commit(
        self, expected_revision: int | None, envelope: StoredEnvelope, fence: StoreFence, now: float
    ) -> CommitOutcome:
        """Persist the whole transition, verifying revision and fence atomically."""
        ...

    def quarantine(
        self,
        expected_revision: int | None,
        envelope: QuarantinedEnvelope,
        fence: StoreFence,
        now: float,
    ) -> CommitOutcome:
        """Atomically replace active state with an explicit blocked record."""
        ...

    def acquire(self, host_id: str, now: float, duration: float) -> StoreFence | None:
        """Acquire an absent or expired lease with a new epoch.

        Any live owner, including the same host, prevents acquisition. Expiry
        is inclusive. Renew an existing lease explicitly instead.
        """
        ...

    def renew(self, fence: StoreFence, now: float, duration: float) -> StoreFence | None:
        """Extend a live matching epoch without shrinking its expiry."""
        ...

    def verify(self, fence: StoreFence, now: float) -> bool:
        """Verify current owner and epoch against authoritative expiry.

        Time before the last successful mutation cannot authorize dispatch.
        """
        ...
