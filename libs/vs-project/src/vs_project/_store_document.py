"""A legacy run's record in ``store.json``, and the reads of it.

A run saved before the step loop kept its whole run envelope in ``store.json``. This release
only reads it, for the migration of such a run into the run log (``vibesys.run.store_migration``)
and to refuse to resume one: nothing writes the file any more, and no host holds a lease on it.

``store.json`` has two formats. Version 1, from older releases, also held the lease of the host
that wrote it (``fence`` and ``observed_at``), and a document that declares no version is version
1. Version 2 moved the lease to a ``lease.json`` beside it, which this release never reads. A
reader takes the record from either and ignores the lease fields of version 1.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vs_project.api.state_store import ObservationFault, StoreRecord

LEGACY_STORE_DOCUMENT_VERSION = 1
STORE_DOCUMENT_VERSION = 2

STORE_FILE = "store.json"
"""The one document of a store, by file name."""

_CONFIG = ConfigDict(
    extra="forbid", frozen=True, strict=True, ser_json_bytes="base64", val_json_bytes="base64"
)


class StoreDocument(BaseModel):
    """``store.json``: the record; version 1, from older releases, also held the lease."""

    model_config = _CONFIG
    # A document that declares no version is the oldest format, as older releases wrote it.
    version: int = Field(
        default=LEGACY_STORE_DOCUMENT_VERSION,
        ge=LEGACY_STORE_DOCUMENT_VERSION,
        le=STORE_DOCUMENT_VERSION,
    )
    record: StoreRecord | None = None
    fence: dict[str, Any] | None = None
    """The lease of version 1: read to accept the document, never used."""
    observed_at: float = Field(default=0, ge=0, allow_inf_nan=False)
    """The lease's time watermark of version 1: read to accept the document, never used."""

    @model_validator(mode="after")
    def _lease_only_in_the_legacy_format(self) -> Self:
        if self.version != LEGACY_STORE_DOCUMENT_VERSION and (
            self.fence is not None or self.observed_at != 0
        ):
            message = f"store.json version {self.version} holds no lease"
            raise ValueError(message)
        return self

    def encode(self) -> bytes:
        """The document's bytes, leaving out the lease fields the current format never has."""
        legacy = self.version == LEGACY_STORE_DOCUMENT_VERSION
        return self.model_dump_json(exclude=None if legacy else {"fence", "observed_at"}).encode()


def record_document(record: StoreRecord | None) -> StoreDocument:
    """The current-format ``store.json`` holding ``record``."""
    return StoreDocument(version=STORE_DOCUMENT_VERSION, record=record)


class StoreReader(ABC):
    """Read a stored record the same way from memory or from serialized storage."""

    def __init__(self, observation_fault_plan: Iterable[ObservationFault | None] = ()) -> None:
        self._observation_faults = iter(
            tuple(
                None if fault is None else ObservationFault(fault)
                for fault in observation_fault_plan
            )
        )
        self._pending_observation: ObservationFault | None = None

    @abstractmethod
    def _transaction(self) -> AbstractContextManager[None]: ...

    @abstractmethod
    def _read_record(self) -> StoreDocument | None:
        """The decoded ``store.json``, or None when there is none; see ``_observation``."""

    @contextmanager
    def _operation(self) -> Iterator[None]:
        """One read: the lock, and this read's place in the observation plan."""
        with self._transaction():
            self._pending_observation = next(self._observation_faults, None)
            try:
                yield
            finally:
                self._pending_observation = None

    def _observation(self, *, exists: bool) -> ObservationFault | None:
        """The observation fault a read applies: READ at the first read, SYNC at the first found file."""
        fault = self._pending_observation
        if fault == ObservationFault.READ or (fault is not None and exists):
            self._pending_observation = None
            return fault
        return None

    def load(self) -> StoreRecord | None:
        """Read the current entire record under the store lock."""
        with self._operation():
            stored = self._read_record()
            return None if stored is None else stored.record
