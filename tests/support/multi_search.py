"""Write a hypothesis ledger into the run.log of a multi run, for read-path tests.

A multi run keeps what its flow published in its run.log: core's state is a fold of the log, and the
search's state is the last whole state the log holds. The server and the chat tools read the
hypothesis ledger through the registration's projector, so a test of those read paths writes the
ledger the same way, without driving a run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.support.logged_run import write_logged_run
from tests.vibesys.orchestration.multi._host import MultiHost, config

from vibesys.orchestration.multi.search.api import multi
from vs_core.testing.builders import initial_state

if TYPE_CHECKING:
    from vibesys.hypothesis.state import HypothesisState
    from vs_project.api import Project


class SearchRecord:
    """The run.log of one multi run, with a whole search state appended for each saved ledger."""

    def __init__(self, project: Project, run_id: str) -> None:
        """Bind the run whose log receives the records."""
        self._project = project
        self._run_id = run_id

    def save(self, search: HypothesisState) -> None:
        """Append a whole search state whose published ledger is ``search``."""
        started = MultiHost.start(config(), restarting=False).run.state
        state = started.model_copy(
            update={"live": started.live.model_copy(update={"published": search.model_dump_json()})}
        )
        core = initial_state()
        core = core.model_copy(
            update={"run": core.run.model_copy(update={"declaration": multi.declaration})}
        )
        write_logged_run(self._project, self._run_id, core, state)


__all__ = ["SearchRecord"]
