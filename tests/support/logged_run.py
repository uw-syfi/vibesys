"""Write a run into its run.log for read-path tests, without driving a run.

A run keeps core's state as a fold of its log and the search's state as the last whole state the
log holds. A test of a reader (the projector, the server, the chat tools) writes those two the
same way: a process that starts the run if the log holds none, then one advance of the search that
logs its whole state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from protobuf import Oneof

from vs_core.api.recovery import seed_message
from vs_project.api import Project
from vs_proto.api import EpochStarted, Record, StrategyAdvanced, Timestamp
from vs_runlog.api import FileRunLogWriter
from vs_sim.api.testing import ManualClock, SimThreads

if TYPE_CHECKING:
    from vs_core.api import CoreState, StrategyState

AT = Timestamp.from_datetime(datetime(2026, 8, 11, 12, 34, 56, tzinfo=UTC))


def write_logged_run(
    project: Project, run_id: str, core: CoreState, strategy: StrategyState
) -> None:
    """Append a process of the next epoch to the run's log: the run's start if it has none, and
    the search's whole state ``strategy``."""
    log = Project.run_log_path_for(project.root, run_id)
    log.parent.mkdir(parents=True, exist_ok=True)
    with FileRunLogWriter(log, clock=ManualClock(), threads=SimThreads()) as writer:
        epoch = writer.epoch + 1
        fresh = writer.sequence == 0

        def stamped(body: Oneof[Literal["epoch_started"], EpochStarted]) -> Record:
            return Record(seq=writer.sequence + 1, epoch=epoch, at=AT, clock=0.0, body=body)

        writer.append(stamped(Oneof("epoch_started", EpochStarted(epoch=epoch))))
        if fresh:
            writer.append(
                Record(
                    seq=writer.sequence + 1,
                    epoch=epoch,
                    at=AT,
                    clock=0.0,
                    body=Oneof("run_started", seed_message(core)),
                )
            )
        writer.append(
            Record(
                seq=writer.sequence + 1,
                epoch=epoch,
                at=AT,
                clock=0.0,
                body=Oneof(
                    "strategy_advanced",
                    StrategyAdvanced(
                        schema_version=1,
                        strategy=Oneof("checkpoint", strategy.model_dump_json().encode()),
                    ),
                ),
            )
        )
