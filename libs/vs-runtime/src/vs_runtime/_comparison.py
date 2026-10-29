"""Comparison baselines: what is measured, how a reading is keyed, and where readings are kept.

A comparison is a mature engine measured with the task's own benchmark beside the input. Its
measurement subject is a ``ComparisonSubject``, never a revision: nothing here is adopted,
listed as an idea or offered as a candidate.

A ``ComparisonReading`` is what a client shows: a measured value with its samples, a failure
with its reason, or "not configured" on a machine that cannot run it. Only a measured reading is
cached. The cache key names what changes the number (the engine's image, how it is served and
benchmarked, and the hardware), so a reading is reused exactly when re-measuring would measure the
same thing. A run only reads the cache (``ComparisonStore.lookup``); measuring is an explicit act.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import statistics
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from vs_decode.api import Unreadable, ValidationOptions, read_json
from vs_project.api import ComparisonInput, atomic_write_bytes

if TYPE_CHECKING:
    from collections.abc import Iterator

MAX_REASON_CHARS = 600
_MAX_RECORD_BYTES = 1 << 20
_MIN_SPREAD_SAMPLES = 2


def context_digest(directory: Path) -> str:
    """The hash of a build context: every file's relative path and bytes, in path order."""
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        digest.update(path.relative_to(directory).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


@contextmanager
def comparison_scratch(base: Path) -> Iterator[Path]:
    """A fresh directory below *base* for one measurement's checkout and plan, removed after it."""
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="run-", dir=base))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


class ComparisonSubject(BaseModel):
    """One comparison, resolved for one machine: the declaration plus what pins its numbers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    spec: ComparisonInput
    identity: str = Field(min_length=1)
    """The engine's image digest, or the hash of the build context its Dockerfile builds from."""
    evaluator: str = Field(min_length=1)
    """The evaluator version that measures it: recorded with the reading, never part of its key,
    so an evaluator bump does not silently force a re-measurement."""
    benchmark: tuple[str, ...]
    """The task's benchmark argv (before the comparison's own arguments), which the key covers."""
    hardware: str = Field(min_length=1)

    @property
    def key(self) -> str:
        """The hex digest of everything that changes this comparison's reading."""
        document = {
            "name": self.spec.name,
            "identity": self.identity,
            "serve": list(self.spec.serve),
            "env": sorted(self.spec.env.items()),
            "benchmark_args": list(self.spec.benchmark_args),
            "benchmark": list(self.benchmark),
            "hardware": self.hardware,
        }
        text = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(text.encode()).hexdigest()


class ComparisonSample(BaseModel):
    """One run of the benchmark against the engine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: FiniteFloat
    host: str | None = Field(default=None, min_length=1, max_length=255)
    """The node the run was timed on, when the executor reported it."""


class ComparisonMeasured(BaseModel):
    """A comparison that was measured: its headline metric over its samples."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["measured"] = "measured"
    metric: str = Field(min_length=1)
    unit: str | None = None
    direction: Literal["max", "min"] | None = None
    samples: tuple[ComparisonSample, ...] = Field(min_length=1)

    @property
    def value(self) -> float:
        """The mean of the samples."""
        return statistics.fmean(sample.value for sample in self.samples)

    @property
    def spread(self) -> float | None:
        """The samples' relative standard deviation, or None for one sample or a zero mean."""
        mean = self.value
        if len(self.samples) < _MIN_SPREAD_SAMPLES or mean == 0:
            return None
        return statistics.stdev(sample.value for sample in self.samples) / abs(mean)

    @property
    def hosts(self) -> tuple[str, ...]:
        """The distinct nodes the samples ran on, in the order they ran."""
        return tuple(dict.fromkeys(s.host for s in self.samples if s.host is not None))


class ComparisonFailed(BaseModel):
    """A comparison that could not be measured; never a reason to stop the search."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["failed"] = "failed"
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS)


class ComparisonNotConfigured(BaseModel):
    """A comparison this machine is not set up to run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["not_configured"] = "not_configured"
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS)


class ComparisonNotMeasured(BaseModel):
    """A comparison nobody measured for its current configuration; a run starts no job for it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["not_measured"] = "not_measured"


ComparisonReading = Annotated[
    ComparisonMeasured | ComparisonFailed | ComparisonNotConfigured | ComparisonNotMeasured,
    Field(discriminator="status"),
]


def bounded_reason(text: str) -> str:
    """*text* as one line of at most ``MAX_REASON_CHARS``; a placeholder when it says nothing."""
    flat = " ".join(text.split())
    if not flat:
        return "no reason was reported"
    return flat if len(flat) <= MAX_REASON_CHARS else flat[: MAX_REASON_CHARS - 1] + "…"


class StoredComparison(BaseModel):
    """A measured reading as the cache holds it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(pattern=r"^[0-9a-f]{64}$")
    name: str
    label: str
    evaluator: str = Field(min_length=1)
    """The evaluator version the reading was measured with."""
    generation: int = Field(ge=0)
    """How many readings this key held before this one; names a re-measurement's job handles."""
    measured_at: float = Field(ge=0)
    reading: ComparisonMeasured


class _Attempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    number: int = Field(ge=0)
    concluded: bool


class ComparisonStore:
    """Readings kept in a directory, one file per key; local to a machine and never committed.

    The directory is the project's machine-local comparison directory
    (``ProjectState.comparison_directory``). An unreadable or foreign file is a miss, never an
    error: a cache entry that cannot be trusted is measured again.
    """

    def __init__(self, directory: Path) -> None:
        """Keep readings below *directory*, which must exist."""
        self._directory = directory

    def _read(self, path: Path) -> StoredComparison | None:
        loaded = read_json(
            path,
            StoredComparison,
            max_bytes=_MAX_RECORD_BYTES,
            options=ValidationOptions(strict=True),
        )
        return None if isinstance(loaded, Unreadable) else loaded

    def get(self, subject: ComparisonSubject) -> StoredComparison | None:
        """The cached reading for *subject*'s key, or None when it was never measured."""
        key = subject.key
        stored = self._read(self._directory / f"{key}.json")
        return stored if stored is not None and stored.key == key else None

    def begin_attempt(self, subject: ComparisonSubject) -> int:
        """The number naming this measurement's jobs: new after a concluded one, the same after a stop.

        A measurement that ended (measured or failed) is never replayed from its recorded jobs, so
        measuring again runs fresh ones; one that was interrupted resumes the jobs it submitted.
        """
        path = self._directory / f"{subject.key}.attempt.json"
        loaded = read_json(
            path, _Attempt, max_bytes=_MAX_RECORD_BYTES, options=ValidationOptions(strict=True)
        )
        last = None if isinstance(loaded, Unreadable) else loaded
        number = 0 if last is None else last.number if not last.concluded else last.number + 1
        self._write_attempt(path, _Attempt(number=number, concluded=False))
        return number

    def conclude_attempt(self, subject: ComparisonSubject, number: int) -> None:
        """Record that attempt *number* ended, however it ended."""
        path = self._directory / f"{subject.key}.attempt.json"
        self._write_attempt(path, _Attempt(number=number, concluded=True))

    @staticmethod
    def _write_attempt(path: Path, attempt: _Attempt) -> None:
        atomic_write_bytes(path, (attempt.model_dump_json() + "\n").encode())

    def lookup(self, subject: ComparisonSubject) -> ComparisonMeasured | ComparisonNotMeasured:
        """What a run shows: the stored reading for the current configuration, else "not measured"."""
        stored = self.get(subject)
        return ComparisonNotMeasured() if stored is None else stored.reading

    def put(
        self, subject: ComparisonSubject, reading: ComparisonMeasured, *, measured_at: float
    ) -> StoredComparison:
        """Keep *reading* as *subject*'s, one generation after the one it replaces."""
        earlier = self.get(subject)
        record = StoredComparison(
            key=subject.key,
            name=subject.spec.name,
            label=subject.spec.label,
            evaluator=subject.evaluator,
            generation=0 if earlier is None else earlier.generation + 1,
            measured_at=measured_at,
            reading=reading,
        )
        atomic_write_bytes(
            self._directory / f"{record.key}.json", (record.model_dump_json() + "\n").encode()
        )
        return record

    def has_measured(self, name: str) -> bool:
        """Whether any key of the comparison called *name* holds a reading."""
        return any(
            stored is not None and stored.name == name
            for stored in (self._read(path) for path in sorted(self._directory.glob("*.json")))
        )


__all__ = [
    "MAX_REASON_CHARS",
    "ComparisonFailed",
    "ComparisonMeasured",
    "ComparisonNotConfigured",
    "ComparisonNotMeasured",
    "ComparisonReading",
    "ComparisonSample",
    "ComparisonStore",
    "ComparisonSubject",
    "StoredComparison",
    "bounded_reason",
    "comparison_scratch",
    "context_digest",
]
