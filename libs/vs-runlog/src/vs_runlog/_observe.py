"""Observation reads of a run: its head, canonical epoch and facts a previous writer left implicit."""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

from vs_proto.api import Record, RunHead, validate
from vs_runlog._epoch import CurrentEpoch, current_epoch
from vs_runlog._file import RunLogReader
from vs_runlog._framing import RunLogStopReason
from vs_runlog._upgrade import InputUpgrade

_MEMO_CAPACITY = 1024


class RunLogDamagedError(ValueError):
    """One run's head or committed log prefix is unreadable; no other run is affected."""

    def __init__(self, path: Path, reason: object) -> None:
        super().__init__(f"{path}: {reason}")


@contextmanager
def _damage_is_typed(path: Path) -> Iterator[None]:
    try:
        yield
    except RunLogDamagedError:
        raise
    except ValueError as error:
        raise RunLogDamagedError(path, error) from error


@dataclass(frozen=True)
class _Prefix:
    """A committed prefix of one log file, which never changes once published."""

    device: int
    inode: int
    modified_ns: int
    size: int
    committed: int | None
    headed: bool = True


@dataclass(frozen=True)
class _Recovered:
    epoch: CurrentEpoch
    input_value: float | None
    project_root: str | None = None


class HeadObserver:
    """Reads run heads, recovering a previous writer's missing facts once per committed prefix.

    A head states its epoch and whether the input is measured when its writer knows them. Only a
    head without them needs the log, and its facts are immutable for its committed prefix, so
    ``scans`` counts the distinct prefixes scanned, not the reads. An external reader's epoch
    folds the same prefix and is remembered the same way.
    """

    def __init__(self, capacity: int = _MEMO_CAPACITY) -> None:
        self.scans = 0
        self._capacity = capacity
        self._memo: OrderedDict[_Prefix, _Recovered | str] = OrderedDict()

    def read(self, path: Path) -> RunHead:
        """Read a validated observation, never recovery state; files stay unchanged."""
        with _damage_is_typed(path):
            head = _read_raw_head(path)
            log = path.with_name("run.log")
            wants_input = (
                head.objective is not None
                and not head.objective.input_stated
                and not head.objective.has_field("input_value")
            )
            if (head.epoch == 0 or wants_input) and log.exists():
                recovered = self._recovered(log, head)
                head.epoch = head.epoch or recovered.epoch.number
                if wants_input and recovered.input_value is not None and head.objective is not None:
                    head.objective.input_value = recovered.input_value
            return head

    def project_root(self, path: Path) -> str | None:
        """Read a retained run's recorded project without requiring an address publication.

        The same identity and commit fence memo as head reads owns this recovery.
        Missing facts remain unknown; no discovery or writer restart is required.
        """
        with _damage_is_typed(path):
            head = _read_raw_head(path) if path.with_name("run.head").exists() else None
            return self._recovered(path, head).project_root if path.exists() else None

    def current_epoch(self, path: Path) -> CurrentEpoch | None:
        """Derive an external reader's epoch from exactly the head's committed prefix.

        A complete frame can precede its fsync and head publication. Native readers
        cannot treat that frame as a published epoch. Phase 1 logs without a head
        retain their legacy complete-record rule.
        """
        head_path = path.parent / "run.head"
        with _damage_is_typed(path):
            head = _read_raw_head(path) if head_path.exists() else None
            if not path.exists():
                if head is not None:
                    raise RunLogDamagedError(path, "run.head exists without run.log")
                return None
            return current_epoch((), self._recovered(path, head).epoch)

    def _recovered(self, log: Path, head: RunHead | None) -> _Recovered:
        key = _prefix(log, head)
        if (known := self._memo.get(key)) is not None:
            self._memo.move_to_end(key)
            if isinstance(known, str):
                raise RunLogDamagedError(log, known)
            return known
        self.scans += 1
        try:
            recovered = _recover_prefix(log, head)
        except ValueError as error:
            self._remember(key, str(error))
            raise
        self._remember(key, recovered)
        return recovered

    def _remember(self, key: _Prefix, fact: _Recovered | str) -> None:
        self._memo[key] = fact
        if len(self._memo) > self._capacity:
            self._memo.popitem(last=False)


def _recover_prefix(log: Path, head: RunHead | None) -> _Recovered:
    facts, upgrade = CurrentEpoch(), InputUpgrade()
    project_root = None
    with log.open("rb") as stream:
        for record in _head_records(RunLogReader(stream), head, log):
            if record.body is not None and record.body.field == "epoch_started":
                project_root = record.body.value.project_root or project_root
            if head is None and not _legacy_epoch_record(record):
                return _Recovered(CurrentEpoch(), None, project_root)
            facts = current_epoch((record,), facts)
            upgrade.apply(record)
    return _Recovered(facts, upgrade.measured, project_root)


def _prefix(log: Path, head: RunHead | None) -> _Prefix:
    stat = log.stat()
    committed = head.committed_seq if head is not None and head.has_field("committed_seq") else None
    return _Prefix(
        stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size, committed, headed=head is not None
    )


_OBSERVER = HeadObserver()


def read_run_head(path: Path) -> RunHead:
    """Read a validated observation with its canonical epoch, never recovery state.

    Supply missing epoch and input caches from the published prefix through the
    store upgrade, never process metadata. Original files stay unchanged. An unreadable
    head or committed prefix raises ``RunLogDamagedError``.
    """
    return _OBSERVER.read(path)


def _head_records(reader: RunLogReader, head: RunHead | None, path: Path) -> Iterator[Record]:
    limit = head.committed_seq if head is not None and head.has_field("committed_seq") else None
    if limit == 0:
        return
    through = 0
    for record in reader:
        if limit is not None and record.seq > limit:
            break
        yield record
        through = record.seq
        if through == limit:
            return
    if reader.stop.reason is RunLogStopReason.CORRUPTION:
        raise RunLogDamagedError(path, reader.stop.detail)
    if limit is not None and through < limit:
        raise RunLogDamagedError(path, f"committed sequence {limit} exceeds log sequence {through}")


def _read_raw_head(path: Path) -> RunHead:
    head = RunHead.from_binary(path.with_name("run.head").read_bytes())
    validate(head)
    return head


def read_current_epoch(path: Path) -> CurrentEpoch | None:
    """Derive an external reader's epoch from exactly the head's committed prefix.

    Each committed prefix of a log is folded once per process, however often it is read.
    """
    return _OBSERVER.current_epoch(path)


def _legacy_epoch_record(record: Record) -> bool:
    body = record.body
    return bool(
        body
        and (
            body.field == "legacy_event"
            or (body.field == "epoch_started" and body.value.run is None)
        )
    )
