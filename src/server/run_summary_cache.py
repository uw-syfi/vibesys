"""The run summary's cache beside the journal: read what is cached, fold what was appended, keep it.

``summarize_journal`` returns the fold of a run's whole journal (``server.run_summary``). The fold
is cached in ``run-summary.json`` next to ``run-events.jsonl``, so a read folds only the bytes the
journal gained since the cache was written, however long the run is.

The cache is derived data: the journal stays the one record of the run, nothing reads the cache
but this module, and a cache that cannot be proven to describe a prefix of the journal is
ignored and rebuilt. It records the journal byte offset it covers (``boundary``, always the end of
a complete line) and fingerprints of the journal at that offset: a digest of the first
``HEAD_BYTES`` and of the last ``TAIL_BYTES`` before it. The journal is append-only, so a cache is
valid when the journal is at least ``boundary`` bytes long and both fingerprints still match. That
holds across:

- a crash while the journal is being appended to: the cache never covers an unterminated last
  line, and a writer's repair of a torn tail truncates only at or after ``boundary``;
- a resume: the writer appends after the last complete line, so the prefix is unchanged;
- a crash while the cache is written: it is replaced atomically, so a reader sees the old cache
  or the new one, and both describe a prefix of the journal;
- a journal replaced by another file whose first or last fingerprint window differs, or cut
  short: a fingerprint mismatch or a short journal is a miss, and the fold starts over;
- a cache of another fold version, or one that does not parse: a miss.

A cache that could not be written costs only time. Two readers racing each write a valid cache;
the later write may cover less, which is valid too. A line the fold cannot read ends the summary
before it, as it ends ``runs.read_events``, and the cache stops there as well.

An in-place rewrite that preserves length and both fingerprint windows is outside the
append-only journal contract; the on-disk cache has the same limit.

Light like the helper: the standard library, ``vs_project.api`` and the light ``server`` modules.

Public surface: ``summarize_journal``, ``HEAD_BYTES``, ``TAIL_BYTES``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    NonNegativeInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from server.host_run_summary import (
    RUN_SUMMARY_FILE,
    InputGate,
    RunTerminal,
    SpanTokens,
    TokenTally,
    TurnReply,
    UsageRecord,
)
from server.run_summary import RUN_SUMMARY_VERSION, RunSummaryFold, Summarized, SummaryState
from server.run_usage import ExecutionStart
from vs_project.api import ProjectError, atomic_write_bytes

if TYPE_CHECKING:
    from pathlib import Path
    from typing import BinaryIO

    from vs_sim.api import Lock

HEAD_BYTES: Final = 4096
"""How much of the journal's start the cache's first fingerprint covers."""
TAIL_BYTES: Final = 64 * 1024
"""How much of the journal before the cache's boundary its second fingerprint covers."""

_FORMAT: Final = "vibesys-run-summary"
_MEMORY_CACHE_MAX_BYTES: Final = 32 * 1024 * 1024

_Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class _CachedJournal(BaseModel):
    """Where in the journal a cache ends, and the fingerprints that say it is still that journal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    boundary: NonNegativeInt
    head_sha256: _Digest
    tail_sha256: _Digest


class _CachedState(BaseModel):
    """A fold's state as written; spans, overview and schemas are the journal's own documents."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    through_sequence: NonNegativeInt
    event_count: NonNegativeInt
    rounds_finished: NonNegativeInt
    spans: tuple[dict[str, Any], ...]
    overview: dict[str, Any] | None
    replies: tuple[TurnReply, ...]
    schemas: dict[str, dict[str, Any]]
    tokens: TokenTally
    usage: tuple[SpanTokens, ...]
    executions: tuple[ExecutionStart, ...]
    records: tuple[UsageRecord, ...]
    input_gates: tuple[InputGate, ...]
    gates_settled: bool
    terminal: RunTerminal | None

    @model_validator(mode="after")
    def _is_a_fold(self) -> _CachedState:
        if not all(isinstance(span.get("id"), str) for span in self.spans):
            message = "a cached span has no id"
            raise ValueError(message)
        if any(reply.schema_id not in self.schemas for reply in self.replies):
            message = "a cached reply names a schema the cache does not hold"
            raise ValueError(message)
        return self


class _Cache(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["vibesys-run-summary"]
    version: int
    journal: _CachedJournal
    state: _CachedState

    @field_validator("version")
    @classmethod
    def _is_this_fold(cls, version: int) -> int:
        if version != RUN_SUMMARY_VERSION:
            message = f"a cache of fold version {version}, not {RUN_SUMMARY_VERSION}"
            raise ValueError(message)
        return version


@dataclass
class _Remembered:
    journal: Path
    cache_identity: tuple[int, int, int, int, int]
    boundary: int
    fingerprints: tuple[str, str]
    fold: RunSummaryFold


class SummaryReader:
    """Reuse one validated fold across pages in one helper process."""

    def __init__(self, lock: Lock) -> None:
        """Start with no remembered run."""
        self._lock = lock
        self._remembered: _Remembered | None = None

    def summarize(self, journal: Path) -> Summarized:
        """Read the current journal and return a fold isolated from later requests."""
        with self._lock:
            summarized, remembered = _summarize_journal(journal, self._remembered)
            self._remembered = remembered
            # The retained fold grows in place on append. A worker pages its return value after
            # this lock is released, so give it its own maps while retaining the validated fold.
            return Summarized(
                RunSummaryFold.from_state(summarized.fold.state()), summarized.journal_bytes
            )


def summarize_journal(journal: Path) -> Summarized:
    """The fold of ``journal`` as it is now; a journal that does not exist holds no events.

    Folds only what the cache does not cover and writes the cache again when the fold covers
    more complete lines than it did.
    """
    summarized, _ = _summarize_journal(journal, None)
    return summarized


def _cache_identity(cache: Path) -> tuple[int, int, int, int, int] | None:
    try:
        stat = cache.stat()
    except OSError:
        return None
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _summarize_journal(
    journal: Path, remembered: _Remembered | None
) -> tuple[Summarized, _Remembered | None]:
    cache = journal.with_name(RUN_SUMMARY_FILE)
    try:
        stream = journal.open("rb")
    except FileNotFoundError:
        return Summarized(RunSummaryFold(), 0), None
    with stream:
        valid_memory = (
            remembered is not None
            and remembered.journal == journal
            and _cache_identity(cache) == remembered.cache_identity
            and _fingerprints(stream, remembered.boundary) == remembered.fingerprints
        )
        restored = (
            (remembered.fold, remembered.boundary)
            if valid_memory and remembered is not None
            else _restore(cache, stream)
        )
        fold, cached = restored if restored is not None else (RunSummaryFold(), 0)
        stream.seek(cached)
        offset = cached
        covered = cached
        for line in stream:
            if not line.endswith(b"\n"):
                # The writer has not finished it: it counts for this read and is never cached.
                covered = offset
                _keep(cache, stream, fold, covered, cached)
                if fold.consume(line, complete=False):
                    offset += len(line)
                return Summarized(fold, offset), None
            if not fold.consume(line):
                break
            offset += len(line)
            covered = offset
        cache_current = _keep(cache, stream, fold, covered, cached)
        identity = _cache_identity(cache)
        fingerprints = _fingerprints(stream, covered)
        if (
            cache_current
            and identity is not None
            and identity[2] <= _MEMORY_CACHE_MAX_BYTES
            and fingerprints is not None
        ):
            return Summarized(fold, covered), _Remembered(
                journal, identity, covered, fingerprints, fold
            )
        return Summarized(fold, covered), None


def _restore(cache: Path, journal: BinaryIO) -> tuple[RunSummaryFold, int] | None:
    """The cached fold and its boundary, when the journal still begins with what it covered."""
    try:
        document = _Cache.model_validate_json(cache.read_bytes())
    except (OSError, ValueError):
        return None
    covered = document.journal
    if _fingerprints(journal, covered.boundary) != (covered.head_sha256, covered.tail_sha256):
        return None
    state = document.state
    return (
        RunSummaryFold.from_state(
            SummaryState(
                through_sequence=state.through_sequence,
                event_count=state.event_count,
                rounds_finished=state.rounds_finished,
                spans=state.spans,
                overview=state.overview,
                replies=state.replies,
                schemas=state.schemas,
                tokens=state.tokens,
                usage=state.usage,
                executions=state.executions,
                records=state.records,
                input_gates=state.input_gates,
                gates_settled=state.gates_settled,
                terminal=state.terminal,
            )
        ),
        covered.boundary,
    )


def _keep(cache: Path, journal: BinaryIO, fold: RunSummaryFold, boundary: int, cached: int) -> bool:
    """Keep ``fold`` at ``boundary`` on disk; return whether the cache covers it."""
    if boundary <= cached:
        return True
    fingerprints = _fingerprints(journal, boundary)
    if fingerprints is None:
        return False
    head, tail = fingerprints
    state = fold.state()
    document = {
        "format": _FORMAT,
        "version": RUN_SUMMARY_VERSION,
        "journal": {"boundary": boundary, "head_sha256": head, "tail_sha256": tail},
        "state": {
            "through_sequence": state.through_sequence,
            "event_count": state.event_count,
            "rounds_finished": state.rounds_finished,
            "spans": list(state.spans),
            "overview": state.overview,
            "replies": [reply.model_dump(mode="json") for reply in state.replies],
            "schemas": state.schemas,
            "tokens": state.tokens.model_dump(mode="json"),
            "usage": [entry.model_dump(mode="json") for entry in state.usage],
            "executions": [start.model_dump(mode="json") for start in state.executions],
            "records": [record.model_dump(mode="json") for record in state.records],
            "input_gates": [gate.model_dump(mode="json") for gate in state.input_gates],
            "gates_settled": state.gates_settled,
            "terminal": None if state.terminal is None else state.terminal.model_dump(mode="json"),
        },
    }
    contents = json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode()
    try:
        atomic_write_bytes(cache, contents)
    except (OSError, ProjectError):
        return False
    return True


def _fingerprints(journal: BinaryIO, boundary: int) -> tuple[str, str] | None:
    """The digests of the journal's first and last bytes before ``boundary``; ``None`` if shorter."""
    size = journal.seek(0, 2)
    if size < boundary:
        return None
    journal.seek(0)
    head = journal.read(min(HEAD_BYTES, boundary))
    start = max(0, boundary - TAIL_BYTES)
    journal.seek(start)
    tail = journal.read(boundary - start)
    if len(head) != min(HEAD_BYTES, boundary) or len(tail) != boundary - start:
        return None
    return hashlib.sha256(head).hexdigest(), hashlib.sha256(tail).hexdigest()
