"""Durable generic lease metering, exported through ``vs_runtime.api``."""

from __future__ import annotations

import json
import math
import os
import threading
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

__all__ = ["SlotLease", "SlotMeter", "SlotMeterError"]


class SlotMeterError(ValueError):
    """A ledger, lease transition, clock value, or persistence operation is invalid."""


class SlotLease(BaseModel):
    """Immutable projection of one durable lease's charged occupancy."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    lease_id: Annotated[str, Field(min_length=1)]
    opened_at_s: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    accounted_until_s: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    closed: bool

    @model_validator(mode="after")
    def _validate_interval(self) -> SlotLease:
        if self.accounted_until_s < self.opened_at_s:
            message = "accounted_until_s must be at least opened_at_s"
            raise ValueError(message)
        return self

    @property
    def charged_minutes(self) -> float:
        """Return occupancy through close, or the last heartbeat when still open."""
        return (self.accounted_until_s - self.opened_at_s) / 60


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    kind: Literal["open", "heartbeat", "close"]
    lease_id: Annotated[str, Field(min_length=1)]
    at_s: Annotated[float, Field(ge=0, allow_inf_nan=False)]


class SlotMeter:
    """Meter leases in an fsynced JSONL ledger with one exclusive writer.

    ``clock`` supplies monotonic elapsed seconds in the same epoch after reload.
    Every acknowledged mutation is durable. Reads replay the ledger, never the
    current clock, so a crash charges an open lease only to its last heartbeat.
    Callers own lease identities and heartbeat scheduling. Closed leases cannot
    reopen; closing one again is idempotent. Independent processes must not write
    the same ledger concurrently.

    Reload repairs an incomplete final JSON fragment from an interrupted append.
    Complete malformed records, unknown keys, and invalid transitions raise
    ``SlotMeterError`` naming the ledger path and record or lease.
    """

    def __init__(self, path: Path, *, clock: Callable[[], float]) -> None:
        self._path = path
        self._clock = clock
        self._lock = threading.RLock()
        self.leases()

    def open(self, lease_id: str) -> SlotLease:
        """Durably open a new lease at the injected clock's current instant."""
        return self._record("open", lease_id)

    def heartbeat(self, lease_id: str) -> SlotLease:
        """Durably advance an open lease's charged occupancy."""
        return self._record("heartbeat", lease_id)

    def close(self, lease_id: str) -> SlotLease:
        """Durably end occupancy, returning the same result on repeated closes."""
        return self._record("close", lease_id)

    def leases(self) -> tuple[SlotLease, ...]:
        """Return all lease projections in durable opening order."""
        with self._lock:
            leases, _ = self._load()
            return tuple(leases.values())

    @property
    def charged_minutes(self) -> float:
        """Derive the total charged minutes directly from the durable ledger."""
        return math.fsum(lease.charged_minutes for lease in self.leases())

    def _record(self, kind: Literal["open", "heartbeat", "close"], lease_id: str) -> SlotLease:
        with self._lock:
            leases, latest_at_s = self._load()
            previous = leases.get(lease_id)
            if kind == "close" and previous is not None and previous.closed:
                return previous
            try:
                record = _Record(kind=kind, lease_id=lease_id, at_s=self._clock())
            except ValidationError as error:
                message = f"{self._path}: {error}"
                raise SlotMeterError(message) from error
            self._apply(leases, record, latest_at_s)
            self._append(record.model_dump_json().encode() + b"\n")
            return leases[lease_id]

    def _load(self) -> tuple[dict[str, SlotLease], float]:
        leases: dict[str, SlotLease] = {}
        latest_at_s = 0.0
        try:
            contents = self._path.read_bytes()
        except FileNotFoundError:
            return leases, latest_at_s
        except OSError as error:
            message = f"{self._path}: cannot read ledger: {error}"
            raise SlotMeterError(message) from error
        valid_end = 0
        lines = contents.splitlines(keepends=True)
        for index, line in enumerate(lines, start=1):
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                if index == len(lines) and not line.endswith(b"\n"):
                    self._truncate(valid_end)
                    return leases, latest_at_s
                message = f"{self._path}: record {index}: {error}"
                raise SlotMeterError(message) from error
            try:
                record = _Record.model_validate(value, strict=True)
                self._apply(leases, record, latest_at_s)
            except (ValidationError, SlotMeterError) as error:
                message = f"{self._path}: record {index}: {error}"
                raise SlotMeterError(message) from error
            latest_at_s = record.at_s
            valid_end += len(line)
        if contents and not contents.endswith(b"\n"):
            self._append(b"\n")
        return leases, latest_at_s

    def _apply(self, leases: dict[str, SlotLease], record: _Record, latest_at_s: float) -> None:
        if record.at_s < latest_at_s:
            message = f"{self._path}: lease {record.lease_id!r}: at_s moved backwards"
            raise SlotMeterError(message)
        previous = leases.get(record.lease_id)
        if record.kind == "open":
            if previous is not None:
                message = f"{self._path}: lease {record.lease_id!r} already exists"
                raise SlotMeterError(message)
            leases[record.lease_id] = SlotLease(
                lease_id=record.lease_id,
                opened_at_s=record.at_s,
                accounted_until_s=record.at_s,
                closed=False,
            )
        else:
            if previous is None or previous.closed:
                message = f"{self._path}: lease {record.lease_id!r} is not open"
                raise SlotMeterError(message)
            leases[record.lease_id] = previous.model_copy(
                update={"accounted_until_s": record.at_s, "closed": record.kind == "close"}
            )

    def _append(self, contents: bytes) -> None:
        try:
            directories = self._prepare_directory()
            created = not self._path.exists()
            with self._path.open("ab") as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            if created:
                for directory in directories:
                    self._sync_directory(directory)
        except OSError as error:
            message = f"{self._path}: cannot append ledger: {error}"
            raise SlotMeterError(message) from error

    def _prepare_directory(self) -> list[Path]:
        """Include each new directory's parent link in the durability barrier."""
        directory = self._path.parent
        directories = [directory]
        while not directory.exists():
            directory = directory.parent
            directories.append(directory)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        return directories

    @staticmethod
    def _sync_directory(path: Path) -> None:
        directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _truncate(self, length: int) -> None:
        try:
            with self._path.open("r+b") as stream:
                stream.truncate(length)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as error:
            message = f"{self._path}: cannot repair ledger tail: {error}"
            raise SlotMeterError(message) from error
