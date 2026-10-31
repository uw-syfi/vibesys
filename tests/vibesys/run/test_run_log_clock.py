"""Every record of a run's log is dated by one clock, so the log folds to the state the loop held.

The loop dates a fact at the run's instant and appends the record that holds it in one
synchronous stretch. The journal's events, and the display records they become, carry the wall
time of the moment they were emitted, which is later. A record dated by that wall time runs ahead
of a fact dated a moment before it, and a fold of the whole log (a resume, the cache the log
keeps) then refuses the fact: ``clock moved backwards``.
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from hypothesis import example, given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter

import vs_core.api as core
from vibesys.events import CoreEventType, RunStartedData, SpanStartedData
from vibesys.run import EventJournal
from vibesys.run.run_clock import start_clock_from
from vs_core.api.recovery import fold_run
from vs_project.api import Project
from vs_runtime.api.step_runner import RunLogStepLog
from vs_runtime.api.testing import fake_run_seed
from vs_sim.api import yield_now
from vs_sim.api.testing import ManualClock, SimThreads, VirtualClock, examples, run_virtual

if TYPE_CHECKING:
    from vs_proto.api import Record

_RUN = "run-a"
_CORE_RECORDS = frozenset({"core_input", "step_started"})
_SPAN = TypeAdapter(core.WireSpan)

type Move = tuple[str, int]

MOVES: st.SearchStrategy[Move] = st.tuples(
    st.sampled_from(["journal", "control", "tick", "wait"]), st.integers(0, 4)
)


class _AgingClock(ManualClock):
    """A wall clock that ages a millisecond each time it is read, as time passes between reads."""

    def now(self) -> float:
        at = super().now()
        self.advance(0.001)
        return at


def _emit_span(journal: EventJournal, number: int) -> None:
    span = _SPAN.validate_python(
        {
            "id": f"op:work.{number}",
            "parent": None,
            "type": "operation",
            "name": "work",
            "status": "running",
            "started_at": 0,
        }
    )
    journal.emit(CoreEventType.SPAN_STARTED, data=SpanStartedData(span=span))


def _control(action: core.ControlAction, now: float, number: int) -> core.RunControlEvent:
    owner = core.PauseOwner.OPERATOR if action == "pause" else None
    when = "now" if action == "resume" else None
    control = core.ControlInput(
        control_id=core.ControlId(root=f"operator-{number}"), action=action, owner=owner, when=when
    )
    return core.RunControlEvent(control=control, now_at=now)


def _play(root: Path, moves: list[Move]) -> None:
    """Run ``moves`` as stretches of one loop, checking the log after each."""
    wall = _AgingClock(start=1000.0)
    journal = EventJournal(wall, SimThreads())
    journal.enable_runlog(Project.open(root), _RUN)
    journal.attach(Project.log_directory_for(root, _RUN), _RUN)
    journal.emit(CoreEventType.RUN_STARTED, data=RunStartedData(outer_loop="test", input="input"))
    log_file = journal.run_log
    assert log_file is not None
    clock = journal.run_clock
    start_clock_from(clock, log_file.records(), journal.epoch)
    seed = fake_run_seed([30.0, 60.0], retries=2)
    log = RunLogStepLog.open(
        log_file,
        epoch=journal.epoch,
        seed=seed.state,
        registry=seed.registry,
        clock=clock,
        stamp=clock.stamp,
    )

    async def main() -> None:
        for number, (name, size) in enumerate(moves):
            now = clock.now()
            match name:
                case "journal":
                    _emit_span(journal, number)
                case "control":
                    _emit_span(journal, number)
                    action: core.ControlAction = "pause" if size % 2 else "resume"
                    with suppress(core.ControlRefusedError):
                        log.input(_control(action, now, number))
                case "tick":
                    _emit_span(journal, number)
                    await log.clock_advanced(now)
                case _:
                    wall.advance(float(size))
            _check(log, log_file.records(), seed.registry)
            await yield_now()

    run_virtual(VirtualClock(), main())
    journal.close()


def _check(
    log: RunLogStepLog, records: tuple[Record, ...], registry: core.OperationRegistry
) -> None:
    """The clocks are in order, the whole log folds, and a core record leaves the loop's state."""
    assert [r.clock for r in records] == sorted(r.clock for r in records)
    folded = fold_run(records, registry=registry)
    last = records[-1].body
    if last is not None and last.field in _CORE_RECORDS:
        assert folded.state == log.state


@settings(max_examples=examples(pr=8, nightly=100), deadline=None)
@given(moves=st.lists(MOVES, min_size=1, max_size=10))
@example(moves=[("control", 1)])
@example(moves=[("tick", 0)])
def test_a_log_folds_to_the_state_the_loop_held_when_the_journal_emits_in_its_stretch(
    moves: list[Move],
) -> None:
    with TemporaryDirectory() as directory:
        _play(Path(directory), moves)
