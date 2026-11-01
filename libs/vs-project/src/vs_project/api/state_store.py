"""A legacy run's stored record, independent of kernel schemas.

A run saved before the step loop serialized its complete kernel envelope into ``payload``. This
release only reads that record, for the migration of the run into the run log; nothing writes it.
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


class ObservationFault(StrEnum):
    """Faults consumed by observations, including internal transaction reads."""

    READ = "read"
    SYNC = "sync"


class StateStore(Protocol):
    """Read the record a legacy run's writer published, with interchangeable implementations.

    I/O observation errors propagate. A document that is damaged, or written by a newer release,
    is a typed error that names its source and never echoes it.
    """

    def observe(self) -> StoreRecord | None:
        """Read one atomic published record without locking or synchronizing it.

        Observation grants no durability. A concurrent publication yields either whole
        version, including a quarantined record.
        """
        ...

    def load(self) -> StoreRecord | None:
        """Load the latest whole record, including a blocked quarantine, synchronized under the lock."""
        ...
