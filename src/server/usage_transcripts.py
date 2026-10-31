"""A person's own CLI use on this host: the provider transcripts, read incrementally into a cache.

The provider CLIs keep a transcript of every session in a directory the host can read. agentshim
owns the formats and the bounded, resumable reading (``vs_agent.api.usage_transcripts``); this
module owns what a host does with it: it keeps the position it reached in each file and the
usage found so far, added up by quarter hour, session, working directory, model and speed, in
one file (``transcript-scan.pb``) that a later read continues from.

- A read costs the walk of the directories plus the bytes it reads, at most a byte budget per
  call. A file it could not finish is ``pending`` and the next call continues it, so a cold
  host with gigabytes of transcripts settles over several calls with the figures filling in.
- A file that changed in place (rewritten, truncated) is read again from the start and the
  rows held for it are dropped. A file that could not be read keeps the rows it had and marks
  its source failed: it is never read as empty.
- Rows older than 92 days are dropped, whatever the files still hold. The cache is derived data:
  one that fails its rules is rebuilt whole, and two hosts writing it at once lose only the
  loser's progress.
- The working directory of a session is a path on this host. It stays in the cache and in the
  exclusion that tells a run's session from a person's; it is in no reply.

Public surface: ``scan_transcripts``, ``OutsideScan``, ``RootReport``, ``SCAN_FILE``,
``SCAN_VERSION``, ``RETENTION`` and ``TRANSCRIPT_BUDGET_BYTES``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final

from server.usage_outside import ProviderRow
from server.usage_rollup import QUARTER_SECONDS
from vs_agent.api import usage_transcripts as transcripts
from vs_project.api import ProjectError, atomic_write_bytes
from vs_proto.api import UsageSpeed, ValidationError, usage_state, validate

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

SCAN_FILE: Final = "transcript-scan.pb"
"""The cache, in the host's usage state directory."""
SCAN_VERSION: Final = 1
"""The version of what the cache holds and how its rows are made; another version is rebuilt."""
RETENTION: Final = timedelta(days=92)
"""How long a row is kept: the longest period the screen shows, and a day of slack."""
TRANSCRIPT_BUDGET_BYTES: Final = 256 * 1024 * 1024
"""The transcript bytes one read may scan, alone or beside the journals it shares a budget with."""

_SPEEDS: Final = {
    "standard": UsageSpeed.STANDARD,
    "fast": UsageSpeed.FAST,
    "ultrafast": UsageSpeed.ULTRAFAST,
}


@dataclass(frozen=True)
class RootReport:
    """How completely one provider's directory was read."""

    provider: str
    root: str
    status: usage_state.UsageSourceStatus
    scanned: int
    skipped: int
    malformed: int
    failed: int


@dataclass(frozen=True)
class OutsideScan:
    """The sessions the transcripts hold, before the runs' own are left out."""

    rows: tuple[ProviderRow, ...]
    reports: tuple[RootReport, ...]
    pending_files: int
    read_bytes: int
    """The bytes this call read, which a caller sharing a budget charges."""


def scan_transcripts(
    directory: Path,
    env: Mapping[str, str],
    *,
    now: datetime,
    since: datetime,
    budget: int,
) -> OutsideScan:
    """Read the transcripts that may hold usage since ``since``, at most about ``budget`` bytes.

    ``directory`` is the host's usage state directory, where the cache lives. ``env`` says where
    each provider keeps its sessions. The cache is written when this call learned something.
    """
    path = directory / SCAN_FILE
    cache = _load(path)
    held = {(root.provider, root.path): root for root in cache.roots}
    roots: list[usage_state.TranscriptRootState] = []
    reports: list[RootReport] = []
    pending = 0
    spent = 0
    for provider in transcripts.providers():
        for root in transcripts.roots(provider, env):
            state = held.get((provider, str(root.path))) or usage_state.TranscriptRootState(
                provider=provider, path=str(root.path)
            )
            left = max(1, budget - spent)
            scan = transcripts.read(root, known=_known(state), since=since, byte_budget=left)
            merged, report, read = _merge(state, scan, root)
            roots.append(merged)
            reports.append(report)
            pending += scan.pending
            spent += read
    kept = _retained(roots, now)
    updated = usage_state.TranscriptScanCache(version=SCAN_VERSION, roots=kept)
    if updated.to_binary() != cache.to_binary():
        _save(path, updated)
    rows = tuple(
        ProviderRow(root.provider, row) for root in kept for each in root.files for row in each.rows
    )
    return OutsideScan(rows, tuple(reports), pending, spent)


def _load(path: Path) -> usage_state.TranscriptScanCache:
    """The cache; an empty one when there is none or it fails its rules or is another version."""
    try:
        found = usage_state.TranscriptScanCache.from_binary(path.read_bytes())
        validate(found)
    except (OSError, ValueError, ValidationError):
        return usage_state.TranscriptScanCache(version=SCAN_VERSION)
    if found.version != SCAN_VERSION:
        return usage_state.TranscriptScanCache(version=SCAN_VERSION)
    return found


def _save(path: Path, cache: usage_state.TranscriptScanCache) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_bytes(path, cache.to_binary())
    except (OSError, ProjectError):
        return


def _known(state: usage_state.TranscriptRootState) -> dict[Path, transcripts.Position]:
    return {
        Path(each.path): transcripts.Position(
            each.size,
            each.mtime_ns,
            each.offset,
            each.guard_length,
            each.guard_hash,
            each.state,
            each.complete,
        )
        for each in state.files
    }


def _merge(
    state: usage_state.TranscriptRootState, scan: transcripts.Scan, root: transcripts.Root
) -> tuple[usage_state.TranscriptRootState, RootReport, int]:
    """The root's state after ``scan``, how it went, and the bytes it read."""
    files = {each.path: each for each in state.files}
    read = 0
    malformed = 0
    for found in scan.files:
        key = str(found.path)
        before = files.get(key)
        fresh = before is None or found.reset
        base = usage_state.TranscriptFileState(path=key) if fresh or before is None else before
        position = found.position
        read += max(0, position.offset - (0 if fresh or before is None else before.offset))
        files[key] = usage_state.TranscriptFileState(
            path=key,
            size=position.size,
            mtime_ns=position.mtime_ns,
            offset=position.offset,
            guard_length=position.guard_length,
            guard_hash=position.guard_hash,
            state=position.state,
            complete=position.complete,
            malformed=found.malformed,
            rows=_added(base.rows, found.records),
        )
    for each in files.values():
        malformed += each.malformed
    merged = usage_state.TranscriptRootState(
        provider=state.provider,
        path=str(root.path),
        files=[files[key] for key in sorted(files)],
    )
    if not scan.found:
        status = usage_state.UsageSourceStatus.MISSING
    elif scan.failed:
        status = (
            usage_state.UsageSourceStatus.FAILED
            if not files
            else usage_state.UsageSourceStatus.PARTIAL
        )
    elif scan.pending:
        status = usage_state.UsageSourceStatus.PARTIAL
    else:
        status = usage_state.UsageSourceStatus.OK
    report = RootReport(
        provider=state.provider,
        root=str(root.path),
        status=status,
        scanned=len(scan.files),
        skipped=scan.skipped,
        malformed=malformed,
        failed=len(scan.failed),
    )
    return merged, report, read


def _added(
    held: object, records: tuple[transcripts.Record, ...]
) -> list[usage_state.TranscriptRow]:
    """The rows held plus ``records`` added in, one row per quarter, session, directory, model and speed."""
    totals: dict[tuple[int, str, str, str, int], list[int]] = {}
    for row in held if isinstance(held, (list, tuple)) else ():
        _into(totals, (row.quarter, row.session_id, row.cwd, row.model, int(row.speed)), row)
    for record in records:
        key = (
            math.floor(record.timestamp.timestamp() / QUARTER_SECONDS),
            record.session_id,
            record.cwd or "",
            record.model,
            int(_SPEEDS.get(record.speed, UsageSpeed.STANDARD)),
        )
        counts = record.tokens
        _into(
            totals,
            key,
            usage_state.TranscriptRow(
                input=counts.input,
                cache_read=counts.cache_read,
                cache_write=counts.cache_write,
                cache_write_1h=counts.cache_write_1h,
                output=counts.output,
                reasoning=counts.reasoning,
                turns=record.turns,
            ),
        )
    return [
        usage_state.TranscriptRow(
            quarter=quarter,
            session_id=session,
            cwd=cwd,
            model=model,
            speed=UsageSpeed(speed),
            input=sums[0],
            cache_read=sums[1],
            cache_write=sums[2],
            cache_write_1h=sums[3],
            output=sums[4],
            reasoning=sums[5],
            turns=sums[6],
        )
        for (quarter, session, cwd, model, speed), sums in sorted(totals.items())
    ]


def _into(
    totals: dict[tuple[int, str, str, str, int], list[int]],
    key: tuple[int, str, str, str, int],
    row: usage_state.TranscriptRow,
) -> None:
    sums = totals.setdefault(key, [0] * 7)
    for index, value in enumerate(
        (
            row.input,
            row.cache_read,
            row.cache_write,
            row.cache_write_1h,
            row.output,
            row.reasoning,
            row.turns,
        )
    ):
        sums[index] += value


def _retained(
    roots: list[usage_state.TranscriptRootState], now: datetime
) -> list[usage_state.TranscriptRootState]:
    """``roots`` without the rows older than ``RETENTION``."""
    oldest = math.floor((now - RETENTION).timestamp() / QUARTER_SECONDS)
    for root in roots:
        for each in root.files:
            each.rows = [row for row in each.rows if row.quarter >= oldest]
    return roots
