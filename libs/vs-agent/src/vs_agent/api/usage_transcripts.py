"""A person's own CLI use on a host, read from the transcripts the provider CLIs keep.

agentshim owns the formats (where each provider keeps its session files and what a line of one
means) and the bounded, resumable reading. This sub-facade is the one place VibeSys code reaches
it for the Usage screen, and no agentshim type crosses it: a position, a record and a scan are the
frozen dataclasses below. The caller keeps the positions it is handed and passes them back, so
reading holds no state of its own and a read over any split of a file's growth, and over any
byte budget, gives the records one read of the whole file gives.

A record carries the session's working directory, a path on this host. It is for the host's own
cache and for telling a run's session from a person's; it never goes into a reply.

Public surface: ``TokenCounts``, ``Record``, ``Position``, ``FileScan``, ``Scan``, ``Root``,
``providers``, ``roots`` and ``read``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentshim.core.transcripts import FilePosition, TranscriptRoot
from agentshim.transcripts import read_transcript_usage, transcript_providers, transcript_roots

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from pathlib import Path

    from agentshim.core.transcripts import FileScan as AgentFileScan
    from agentshim.core.transcripts import TranscriptRecord


@dataclass(frozen=True)
class TokenCounts:
    """The tokens of some requests; input counts cache reads and writes, reasoning is part of output."""

    input: int
    cache_read: int
    cache_write: int
    cache_write_1h: int
    output: int
    reasoning: int


@dataclass(frozen=True)
class Record:
    """One request's usage, or what a later line of the file added to it."""

    timestamp: datetime
    model: str
    session_id: str
    cwd: str | None
    tokens: TokenCounts
    turns: int
    speed: str
    """``standard``, ``fast`` or ``ultrafast``."""


@dataclass(frozen=True)
class Position:
    """Where a read of one file stopped, and what is needed to continue it; kept as it is."""

    size: int
    mtime_ns: int
    offset: int
    guard_length: int
    guard_hash: int
    state: bytes
    complete: bool


@dataclass(frozen=True)
class FileScan:
    """What one read found in one file."""

    path: Path
    position: Position
    records: tuple[Record, ...]
    """Requests after the known position, or in the whole file when ``reset``; each is final."""
    reset: bool
    """The file was rewritten or truncated: drop every record held for this path."""
    malformed: int
    """Unusable lines over the whole file so far: it replaces the caller's count."""


@dataclass(frozen=True)
class Scan:
    """The result of one bounded read of a root."""

    files: tuple[FileScan, ...] = ()
    unchanged: int = 0
    skipped: int = 0
    pending: int = 0
    """Files the byte budget left unfinished: call again with the new positions."""
    failed: tuple[Path, ...] = ()
    found: bool = True


@dataclass(frozen=True)
class Root:
    """A directory a provider keeps transcripts under."""

    provider: str
    path: Path


def providers() -> tuple[str, ...]:
    """The providers whose transcripts can be read."""
    return transcript_providers()


def roots(provider: str, env: Mapping[str, str]) -> tuple[Root, ...]:
    """Where ``provider`` keeps transcripts for the environment ``env``; the directories may not exist."""
    return tuple(Root(each.provider, each.path) for each in transcript_roots(provider, env))


def read(
    root: Root,
    *,
    known: Mapping[Path, Position],
    since: datetime,
    byte_budget: int,
) -> Scan:
    """Read the transcripts under ``root`` that may hold usage since ``since``, at most about ``byte_budget`` bytes.

    A file inside the window is read whole, so its records can be older than ``since``: filter on
    the record's time to count the window. The first file read always makes progress, so calling
    again with the returned positions finishes for any budget.
    """
    scan = read_transcript_usage(
        TranscriptRoot(root.provider, root.path),
        known={path: _agent_position(each) for path, each in known.items()},
        since=since,
        byte_budget=byte_budget,
        include_cwd=True,
    )
    return Scan(
        files=tuple(_file(each) for each in scan.files),
        unchanged=scan.unchanged,
        skipped=scan.skipped,
        pending=scan.pending,
        failed=tuple(scan.failed),
        found=scan.found,
    )


def _agent_position(position: Position) -> FilePosition:
    return FilePosition(
        size=position.size,
        mtime_ns=position.mtime_ns,
        offset=position.offset,
        guard_length=position.guard_length,
        guard_hash=position.guard_hash,
        state=position.state,
        complete=position.complete,
    )


def _file(scan: AgentFileScan) -> FileScan:
    position = scan.position
    return FileScan(
        path=scan.path,
        position=Position(
            position.size,
            position.mtime_ns,
            position.offset,
            position.guard_length,
            position.guard_hash,
            position.state,
            position.complete,
        ),
        records=tuple(_record(each) for each in scan.records),
        reset=scan.reset,
        malformed=scan.malformed,
    )


def _record(record: TranscriptRecord) -> Record:
    tokens = record.tokens
    return Record(
        timestamp=record.timestamp,
        model=record.model,
        session_id=record.session_id,
        cwd=record.cwd,
        tokens=TokenCounts(
            tokens.input_tokens,
            tokens.cache_read_input_tokens,
            tokens.cache_write_input_tokens,
            tokens.cache_write_1h_input_tokens,
            tokens.output_tokens,
            tokens.reasoning_output_tokens,
        ),
        turns=tokens.turns,
        speed=record.speed.value,
    )
