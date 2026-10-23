"""Read a committed native digest through the store library's semantic boundary.

Public surface: ``read_digest``. Keyframes accelerate the read; only the head's published
prefix is visible. Every decoded skeleton passes through the shared retained-store upgrade.
"""

from collections.abc import Iterator
from pathlib import Path

from server.host_api import RunDigest
from server.run_digest import digest_store
from vs_proto.api import Keyframe, Record, ValidationError, validate
from vs_runlog.api import (
    RunLogDamagedError,
    RunLogReader,
    RunLogStopReason,
    fold_skeleton,
    read_run_head,
    upgrade_skeleton,
)

_ACTIVITY_RECORD_LIMIT = 256


def read_digest(path: Path) -> RunDigest:
    """Project one coherent prefix without opening a writer or changing retained bytes."""
    try:
        return _read_digest(path)
    except (ValidationError, RunLogDamagedError) as exc:
        message = f"{path}: invalid committed store"
        raise ValueError(message) from exc


def _read_digest(path: Path) -> RunDigest:
    head = read_run_head(path)
    limit = head.committed_seq if head.has_field("committed_seq") else None
    candidates = sorted(
        (
            (int(candidate.stem), candidate)
            for candidate in (path.parent / "keyframes").glob("*.pb")
            if candidate.stem.isdecimal() and (limit is None or int(candidate.stem) <= limit)
        ),
        reverse=True,
    )
    # Recover the final envelope's activity time only from a bounded suffix. Older
    # conversions may keep just a final keyframe; their optional activity time stays unknown.
    if candidates and candidates[0][0] == limit:
        previous = candidates[1][0] if len(candidates) > 1 else 0
        if limit - previous <= _ACTIVITY_RECORD_LIMIT:
            candidates = candidates[1:]
    seed = None
    if candidates:
        sequence, candidate = candidates[0]
        seed = Keyframe.from_binary(candidate.read_bytes())
        validate(seed)
        if seed.through_seq != sequence or seed.log_offset > path.stat().st_size:
            message = f"{candidate}: keyframe does not match the committed log"
            raise ValueError(message)
    with path.open("rb") as stream:
        reader = RunLogReader(stream, 0 if seed is None else seed.log_offset)
        records = list(_committed(reader, limit, 0 if seed is None else seed.through_seq))
        state = fold_skeleton(records, seed=seed, log_offset=0)
        state.log_offset = reader.stop.offset
    last = records[-1].at if records else None
    state = upgrade_skeleton(state)
    return digest_store(head, state, last_event_at=None if last is None else last.to_datetime())


def _committed(reader: RunLogReader, limit: int | None, through: int) -> Iterator[Record]:
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
        message = f"invalid committed prefix at sequence {through}"
        raise ValueError(message)
