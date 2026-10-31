"""state.pb changes no result: a start from the cache is the start from the whole log.

The step log writes its cache when the run comes to rest and every so often. Whatever the run did
before the process died, the log it left and the cache beside it give a next process the state a
fold of the log gives, and what the search still has to hear. A cache that is gone, damaged, ahead
of the log, or of another log is ignored, and the whole log is folded.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.recovery import (
    Cancelled,
    CancelReason,
    Folded,
    Interruption,
    Run,
    RunRest,
    at_rest,
    decide,
)
from vs_proto.api import EpochStarted, Notice, NoticeCode, Oneof, Record, Severity, Timestamp
from vs_runlog.api import CachedFold, CacheMiss, ConfirmedLog, MemoryFoldCache, PrefixDigest
from vs_runtime.api.step_runner import CACHE_EVERY, CacheUsed, RunLogStepLog
from vs_runtime.api.testing import FakeRunRecords, fake_run_seed
from vs_sim.api.testing import VirtualClock, examples, run_virtual

if TYPE_CHECKING:
    from vs_runtime.api.testing import FakeRunSeed


class RecordingCache(MemoryFoldCache):
    """A cache that remembers every cache it was given, as the disk of each moment of the run."""

    def __init__(self) -> None:
        super().__init__()
        self.history: list[CachedFold] = []

    def store(self, cached: CachedFold) -> None:
        super().store(cached)
        self.history.append(cached)


class HearingSearch:
    """A search that hears nothing and answers nothing, and keeps the folds it was offered."""

    def __init__(self) -> None:
        self.offered: list[Folded] = []

    def advance(self, folded: Folded, now: float) -> None:
        del now
        self.offered.append(folded)

    def rest(self) -> None:
        return None

    def appended(self) -> None:
        return None


@dataclass
class Process:
    """A run.log kept in memory, the cache beside it, and the means to start a process on them."""

    seed: FakeRunSeed
    clock: VirtualClock = field(default_factory=VirtualClock)
    records: FakeRunRecords = field(default_factory=FakeRunRecords)
    cache: RecordingCache = field(default_factory=RecordingCache)
    confirmed: ConfirmedLog | None = None
    """The log of the process started last, which the display projection appends through too."""

    def start(
        self, *, cached: bool = True, every: int = CACHE_EVERY
    ) -> tuple[RunLogStepLog, HearingSearch]:
        """A new process: it opens the next epoch and takes over what the earlier ones left."""
        confirmed = self.confirmed = ConfirmedLog(self.records)
        epoch = (self.records.records[-1].epoch if self.records.records else 0) + 1
        confirmed.append(
            EpochStarted(epoch=epoch), epoch=epoch, at=Timestamp(), clock=self.clock.now()
        )
        search = HearingSearch()
        log = RunLogStepLog.open(
            confirmed,
            epoch=epoch,
            seed=self.seed.state,
            registry=self.seed.registry,
            clock=self.clock,
            search=search,
            cache=self.cache if cached else None,
            cache_every=every,
        )
        return log, search


def process(steps: int = 3) -> Process:
    return Process(fake_run_seed([30.0 + 10 * index for index in range(steps)], retries=2))


def operator(action: core.ControlAction, now: float, number: int) -> core.RunControlEvent:
    owner = core.PauseOwner.OPERATOR if action == "pause" else None
    when = "now" if action == "resume" else None
    control = core.ControlInput(
        control_id=core.ControlId(root=f"operator-{number}"), action=action, owner=owner, when=when
    )
    return core.RunControlEvent(control=control, now_at=now)


type Move = tuple[str, int]

MOVES: st.SearchStrategy[Move] = st.tuples(
    st.sampled_from(
        ["start", "finish", "raise", "pause", "pause_now", "resume", "restart", "display", "wait"]
    ),
    st.integers(0, 3),
)


def display(world: Process) -> None:
    """A record that is not core's, as the projection of the journal appends between core's."""
    assert world.confirmed is not None
    epoch = world.records.records[-1].epoch
    notice = Notice(
        severity=Severity.WARNING, code=NoticeCode.FRAMEWORK_WARNING, message="x", span=""
    )
    world.confirmed.stage(
        Record(epoch=epoch, at=Timestamp(), clock=world.clock.now(), body=Oneof("notice", notice))
    )
    world.confirmed.commit()


@dataclass
class Driver:
    """What a loop, an operator and the projection do to a run's log, one move at a time."""

    world: Process
    every: int
    log: RunLogStepLog = field(init=False)

    def __post_init__(self) -> None:
        self.log, _ = self.world.start(every=self.every)

    async def move(self, name: str, index: int, number: int) -> None:
        now = self.world.clock.now()
        match name:
            case "start":
                runs = [a for a in decide(self.log.state, now) if isinstance(a, Run)]
                if runs:
                    await self.log.step_started(runs[index % len(runs)])
            case "finish" | "raise":
                await self.end_an_attempt(index, finish=name == "finish")
            case "pause" | "pause_now" | "resume":
                with suppress(core.ControlRefusedError):
                    self.log.input(operator(name, now, number))
            case "restart":
                self.log, _ = self.world.start(every=self.every)
            case "display":
                display(self.world)
            case _:
                await self.world.clock.sleep(10.0 * (index + 1))

    async def end_an_attempt(self, index: int, *, finish: bool) -> None:
        running = [row for row in self.log.state.steps.attempts if not row.ended]
        if not running:
            return
        row = running[index % len(running)]
        if finish:
            result = Cancelled(reason=CancelReason.WITHDRAWN)
            await self.log.step_finished(row.tag, row.started, result)
        else:
            await self.log.attempt_interrupted(row.tag, row.started, Interruption.RAISED)


def play(world: Process, moves: list[Move], every: int) -> RunLogStepLog:
    """The moves of a run on ``world``'s log, and the log of the last process."""
    driver = Driver(world, every)

    async def main() -> None:
        for number, (name, index) in enumerate(moves):
            await driver.move(name, index, number)

    run_virtual(world.clock, main())
    return driver.log


@settings(max_examples=examples(pr=10, nightly=200), deadline=None)
@given(
    moves=st.lists(MOVES, max_size=14), every=st.sampled_from([1, 3, CACHE_EVERY]), data=st.data()
)
def test_a_start_from_the_cache_is_a_start_from_the_whole_log(
    moves: list[Move], every: int, data: st.DataObject
) -> None:
    """Whenever the process died and whatever cache it had written by then."""
    world = process()
    play(world, moves, every)
    total = len(world.records.records)
    cut = data.draw(st.integers(1, total), label="records the dead process made durable")
    survivors = world.records.records[:cut]
    written = [c for c in world.cache.history if c.through_seq <= cut]
    after = Process(world.seed, world.clock, _copy(survivors))
    if written:
        after.cache.store(written[-1])
    cached, heard_cached = after.start(every=every)
    plain, heard_plain = Process(world.seed, world.clock, _copy(survivors)).start(cached=False)
    assert cached.state == plain.state
    assert heard_cached.offered == heard_plain.offered
    assert isinstance(cached.cache_start, CacheUsed) == bool(written)


def _copy(survivors: list[Record]) -> FakeRunRecords:
    copy = FakeRunRecords()
    copy.records.extend(survivors)
    return copy


def test_a_run_that_parks_leaves_a_cache_of_its_whole_log_for_the_next_start() -> None:
    world = process(1)
    log, _ = world.start()

    async def main() -> None:
        run = next(a for a in decide(log.state, world.clock.now()) if isinstance(a, Run))
        await log.step_started(run)
        log.input(operator("pause_now", world.clock.now(), 1))

    run_virtual(world.clock, main())
    assert at_rest(log.state) is RunRest.PARKED
    (written,) = world.cache.history[-1:]
    assert written.through_seq == len(world.records.records)
    restarted, _ = world.start()
    assert restarted.cache_start == CacheUsed(written.through_seq)


def test_the_cache_is_written_every_so_many_records_while_the_run_goes_on() -> None:
    world = process(3)
    log, _ = world.start(every=2)

    async def main() -> None:
        for run in [a for a in decide(log.state, world.clock.now()) if isinstance(a, Run)]:
            await log.step_started(run)
            await log.attempt_interrupted(run.tag, run.attempt, Interruption.RAISED)

    run_virtual(world.clock, main())
    assert at_rest(log.state) is None
    assert [c.through_seq for c in world.cache.history] == sorted(
        {c.through_seq for c in world.cache.history}
    )
    assert len(world.cache.history) == log.records_appended // 2


def test_a_start_with_no_cache_says_so() -> None:
    log, _ = process().start(cached=False)
    assert log.cache_start is CacheMiss.ABSENT


def damaged(world: Process, how: str) -> None:
    """Leave in the place of the cache what a crash, a bad disk or another run would."""
    good = world.cache.history[-1]
    match how:
        case "deleted":
            world.cache.damage(None)
        case "garbage":
            world.cache.damage(b"not a cache")
        case "ahead":
            world.cache.store(CachedFold(good.through_seq + 5, good.digest, good.state))
        case "another log":
            world.cache.store(CachedFold(good.through_seq, bytes(32), good.state))
        case "state nobody can read":
            digest = PrefixDigest()
            digest.extend(world.records.records[: good.through_seq])
            world.cache.store(CachedFold(good.through_seq, digest.value(), b"{}"))
        case _:
            raise AssertionError(how)


@pytest.mark.parametrize(
    ("how", "reason"),
    [
        ("deleted", CacheMiss.ABSENT),
        ("garbage", CacheMiss.UNREADABLE),
        ("ahead", CacheMiss.AHEAD_OF_LOG),
        ("another log", CacheMiss.DIGEST_MISMATCH),
        ("state nobody can read", CacheMiss.STATE_REFUSED),
    ],
)
def test_a_cache_that_cannot_be_used_is_ignored_for_a_typed_reason_and_changes_nothing(
    how: str, reason: CacheMiss
) -> None:
    world = process(2)
    log, _ = world.start()

    async def main() -> None:
        run = next(a for a in decide(log.state, world.clock.now()) if isinstance(a, Run))
        await log.step_started(run)
        log.input(operator("pause_now", world.clock.now(), 1))

    run_virtual(world.clock, main())
    damaged(world, how)
    plain, heard_plain = Process(world.seed, world.clock, _copy(world.records.records)).start(
        cached=False
    )
    cached, heard_cached = world.start()
    assert cached.cache_start is reason
    assert cached.state == plain.state
    assert heard_cached.offered == heard_plain.offered
