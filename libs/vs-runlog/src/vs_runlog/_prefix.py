"""A run's committed prefix, folded once from its nearest keyframe.

A reader that wants the head and the skeleton of a run (a list row's digest) used to ask for each
on its own: the head by decoding and validating every record of the log, then the skeleton by
folding the records after a keyframe. Each read is a new process, so nothing remembered the first
scan, and the cost of a launch grew with the history of every run. Here the skeleton is folded
once, from the keyframe, and the head a reader observes is derived from that fold: the same facts
the full scan recovers, for the cost of the records after the keyframe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_proto.api import Keyframe, RunHead, Timestamp, validate
from vs_runlog._epoch import current_epoch
from vs_runlog._file import RunLogReader
from vs_runlog._fold import append_skeleton
from vs_runlog._framing import RunLogStopReason
from vs_runlog._observe import (
    RunLogDamagedError,
    _damage_is_typed,
    _read_raw_head,
    _Recovered,
    apply_recovered,
    needs_recovery,
)
from vs_runlog._upgrade import upgrade_in_place

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime
    from pathlib import Path

    from vs_proto.api import Record

_ACTIVITY_RECORD_LIMIT = 256


@dataclass(frozen=True)
class CommittedPrefix:
    """What a reader sees of one run at its committed sequence, and nothing past it."""

    head: RunHead
    """The head ``read_run_head`` returns for the prefix: stated facts first, recovered ones next."""
    skeleton: Keyframe
    """The prefix's skeleton after the retained-store upgrade."""
    last_record_at: datetime | None
    """When the last record read after the keyframe was written; unknown when none was read."""


def read_committed_prefix(path: Path) -> CommittedPrefix:
    """The committed prefix of the run whose log is ``path``, from its nearest keyframe.

    The cost is the records after that keyframe, not the log. Damage before the keyframe is left
    to the readers that scan the whole prefix (``read_run_head``). Neither file changes. A head,
    keyframe or record that cannot be read raises ``RunLogDamagedError``.
    """
    with _damage_is_typed(path):
        stated = _read_raw_head(path)
        limit = stated.committed_seq if stated.has_field("committed_seq") else None
        seed = _seed(path, limit)
        # The seed was decoded here and nothing else holds it, so the fold and the upgrade extend
        # it in place instead of copying a skeleton the size of the run.
        skeleton = Keyframe(failures_tracked=True, records_tracked=True) if seed is None else seed
        last: Timestamp | None = None
        with path.open("rb") as stream:
            reader = RunLogReader(stream, skeleton.log_offset)
            for record in _committed(path, reader, limit, skeleton.through_seq):
                append_skeleton(skeleton, record)
                last = record.at
            skeleton.log_offset = reader.stop.offset
        measured = upgrade_in_place(skeleton)
        head = stated
        if needs_recovery(stated):
            recovered = _Recovered(current_epoch(skeleton), measured, skeleton.head)
            head = apply_recovered(stated, recovered)
        return CommittedPrefix(head, skeleton, None if last is None else last.to_datetime())


def _seed(path: Path, limit: int | None) -> Keyframe | None:
    """The latest keyframe within the committed prefix, leaving a bounded suffix to read."""
    candidates = sorted(
        (
            (int(candidate.stem), candidate)
            for candidate in (path.parent / "keyframes").glob("*.pb")
            if candidate.stem.isdecimal() and (limit is None or int(candidate.stem) <= limit)
        ),
        reverse=True,
    )
    # The final envelope's activity time comes only from a bounded suffix. An older conversion may
    # keep just a final keyframe; its optional activity time stays unknown.
    if candidates and candidates[0][0] == limit:
        previous = candidates[1][0] if len(candidates) > 1 else 0
        if limit - previous <= _ACTIVITY_RECORD_LIMIT:
            candidates = candidates[1:]
    if not candidates:
        return None
    sequence, candidate = candidates[0]
    seed = Keyframe.from_binary(candidate.read_bytes())
    validate(seed)
    if seed.through_seq != sequence or seed.log_offset > path.stat().st_size:
        raise RunLogDamagedError(path, f"{candidate}: keyframe does not match the committed log")
    return seed


def _committed(
    path: Path, reader: RunLogReader, limit: int | None, through: int
) -> Iterator[Record]:
    if limit is not None and through == limit:
        return
    for record in reader:
        if limit is not None and record.seq > limit:
            break
        yield record
        through = record.seq
        if through == limit:
            return
    if reader.stop.reason is RunLogStopReason.CORRUPTION or (limit is not None and through < limit):
        raise RunLogDamagedError(path, f"invalid committed prefix at sequence {through}")
