"""A run that core has parked is never shown as draining, whatever record the process died after.

Core's fold says a run is parked; the display's lifecycle record says what a reader of the run's
head sees. Two records say one fact, so a process that died between them left a parked run shown
as draining. The log now takes the display's record first, so every prefix of the log (every
instant a process can die at) that folds to a parked run also shows it parked.
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vibesys.events import CoreEventType, RunStartedData
from vibesys.run import EventJournal
from vibesys.run.run_clock import start_clock_from
from vs_core.api.recovery import (
    Cancelled,
    CancelReason,
    Interruption,
    Run,
    RunRest,
    at_rest,
    decide,
)
from vs_project.api import Project
from vs_runlog.api import current_epoch
from vs_runtime.api.step_runner import RunLogStepLog, fold_log
from vs_runtime.api.testing import fake_run_seed
from vs_sim.api import yield_now
from vs_sim.api.testing import ManualClock, SimThreads, VirtualClock, examples, run_virtual

if TYPE_CHECKING:
    from vs_proto.api import Record

_RUN = "run-a"

type Move = tuple[str, int]

MOVES: st.SearchStrategy[Move] = st.tuples(
    st.sampled_from(["start", "finish", "raise", "pause", "pause_now", "resume", "wait"]),
    st.integers(0, 3),
)


def _control(action: core.ControlAction, now: float, number: int) -> core.RunControlEvent:
    owner = core.PauseOwner.OPERATOR if action == "pause" else None
    when = "now" if action == "resume" else None
    control = core.ControlInput(
        control_id=core.ControlId(root=f"operator-{number}"), action=action, owner=owner, when=when
    )
    return core.RunControlEvent(control=control, now_at=now)


def _play(root: Path, moves: list[Move]) -> tuple[Record, ...]:
    """The records of a run that ``moves`` drive, with the display in the log beside core."""
    wall = ManualClock(start=1000.0)
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
        announcer=journal,
    )

    async def main() -> None:
        for number, (name, index) in enumerate(moves):
            now = clock.now()
            match name:
                case "start":
                    runs = [a for a in decide(log.state, now) if isinstance(a, Run)]
                    if runs:
                        await log.step_started(runs[index % len(runs)])
                case "finish" | "raise":
                    running = [row for row in log.state.steps.attempts if not row.ended]
                    if running:
                        row = running[index % len(running)]
                        if name == "finish":
                            cancelled = Cancelled(reason=CancelReason.WITHDRAWN)
                            await log.step_finished(row.tag, row.started, cancelled)
                        else:
                            await log.attempt_interrupted(row.tag, row.started, Interruption.RAISED)
                case "pause" | "pause_now" | "resume":
                    with suppress(core.ControlRefusedError):
                        log.input(_control(name, now, number))
                    if log.state.run.status is core.RunStatus.DRAINING:
                        journal.emit(CoreEventType.PARK_REQUESTED)
                case _:
                    wall.advance(float(index + 1))
            await yield_now()

    run_virtual(VirtualClock(), main())
    records = log_file.records()
    journal.close()
    return records


def _shown_parked(records: tuple[Record, ...]) -> bool:
    lifecycle = current_epoch(records).lifecycle
    return (
        lifecycle is not None and lifecycle.state is not None and lifecycle.state.field == "parked"
    )


def _check(records: tuple[Record, ...], registry: core.OperationRegistry) -> None:
    """Every prefix, the instants a process can die at, shows a parked run as parked."""
    for through in range(1, len(records) + 1):
        prefix = records[:through]
        folded = fold_log(prefix, registry)
        if folded is not None and at_rest(folded.state) is RunRest.PARKED:
            assert _shown_parked(prefix), f"a parked run is not shown parked after record {through}"


@settings(max_examples=examples(pr=8, nightly=100), deadline=None)
@given(moves=st.lists(MOVES, min_size=1, max_size=12))
@example(moves=[("pause_now", 0)])
@example(moves=[("start", 0), ("pause", 0), ("raise", 0)])
@example(moves=[("start", 0), ("pause", 0), ("finish", 0), ("resume", 0), ("pause_now", 0)])
def test_every_prefix_of_the_log_that_folds_to_a_parked_run_shows_it_parked(
    moves: list[Move],
) -> None:
    registry = fake_run_seed([30.0, 60.0], retries=2).registry
    with TemporaryDirectory() as directory:
        _check(_play(Path(directory), moves), registry)


def _lifecycle(records: tuple[Record, ...]) -> list[tuple[int, str]]:
    """The sequence number and state of each lifecycle record the display wrote."""
    shown = []
    for record in records:
        body = record.body
        if body is not None and body.field == "lifecycle" and body.value.state is not None:
            shown.append((record.seq, body.value.state.field))
    return shown


def test_a_run_parked_outright_shows_its_drain_and_then_its_park_before_core_records_it() -> None:
    with TemporaryDirectory() as directory:
        records = _play(Path(directory), [("pause_now", 0)])
    shown = _lifecycle(records)
    (parked,) = [seq for seq, field in shown if field == "parked"]
    assert [field for _, field in shown][-2:] == ["draining", "parked"]
    pause = next(r.seq for r in records if r.body is not None and r.body.field == "core_input")
    assert pause > parked
