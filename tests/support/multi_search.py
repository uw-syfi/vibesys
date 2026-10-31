"""Write a hypothesis ledger into the run.log of a multi run, for read-path tests.

A multi run keeps what its flow published in its run.log: core's state is a fold of the log, and the
search's state is the last whole state the log holds. The server and the chat tools read the
hypothesis ledger through the registration's projector, so a test of those read paths writes the
ledger the same way, without driving a run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from protobuf import Oneof
from tests.vibesys.orchestration.multi._host import MultiHost, config

from vibesys.orchestration.multi.search.api import multi
from vs_core.api.recovery import seed_message
from vs_core.testing.builders import initial_state
from vs_project.api import Project
from vs_proto.api import EpochStarted, Record, StrategyAdvanced, Timestamp
from vs_runlog.api import FileRunLogWriter
from vs_sim.api.testing import ManualClock, SimThreads

if TYPE_CHECKING:
    from vibesys.hypothesis.state import HypothesisState

AT = Timestamp.from_datetime(datetime(2026, 8, 11, 12, 34, 56, tzinfo=UTC))


class SearchRecord:
    """The run.log of one multi run, with a whole search state appended for each saved ledger."""

    def __init__(self, project: Project, run_id: str) -> None:
        """Bind the run whose log receives the records."""
        self._project = project
        self._run_id = run_id

    def save(self, search: HypothesisState) -> None:
        """Append a process that starts the run if the log holds none, then a whole search state."""
        started = MultiHost.start(config(), restarting=False).run.state
        state = started.model_copy(
            update={"live": started.live.model_copy(update={"published": search.model_dump_json()})}
        )
        core = initial_state()
        core = core.model_copy(
            update={"run": core.run.model_copy(update={"declaration": multi.declaration})}
        )
        log = Project.run_log_path_for(self._project.root, self._run_id)
        log.parent.mkdir(parents=True, exist_ok=True)
        with FileRunLogWriter(log, clock=ManualClock(), threads=SimThreads()) as writer:
            epoch = writer.epoch + 1
            bodies = [Oneof("epoch_started", EpochStarted(epoch=epoch))]
            if writer.sequence == 0:
                bodies.append(Oneof("run_started", seed_message(core)))
            bodies.append(
                Oneof(
                    "strategy_advanced",
                    StrategyAdvanced(
                        schema_version=1,
                        strategy=Oneof("checkpoint", state.model_dump_json().encode()),
                    ),
                )
            )
            for body in bodies:
                writer.append(
                    Record(seq=writer.sequence + 1, epoch=epoch, at=AT, clock=0.0, body=body)
                )


__all__ = ["SearchRecord"]
