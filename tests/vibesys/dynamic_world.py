"""The dynamic search's world on the production shell: what its agents reply and measure.

`DynamicWorld` is a vs-dst `World`: the search, its config and the scripted far side of its run.
A test builds one from plain data, simulates it and asserts on the run::

    world = DynamicWorld(throughputs=(120.0,))
    run = simulate(world)

Keyword arguments that name no world option configure the search (`DynamicConfig`), so a
misspelling is refused by name.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from tests.support.checkpoint_faults import FaultyCheckpoints
from tests.support.dynamic_script import Executors, config, harness
from tests.vibesys.orchestration.dynamic._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)

from vibesys.orchestration.dynamic.search.api import dynamic
from vs_dst.api import RunSetup
from vs_runtime.api.testing import Launch

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_core.api import CoreState, Request
    from vs_core.testing.drive import Answer


class DynamicWorld:
    """The planner, implementer, judge and evaluators of one dynamic run.

    A reply is handed out once, in the order requests are authorized, so a copy of the world
    taken mid-run continues from where the run stood.
    """

    def __init__(
        self,
        *,
        planner: Sequence[str | Answer] | None = None,
        implementer: Sequence[str | Answer] | None = None,
        judge: Sequence[str | Answer] | None = None,
        throughputs: Sequence[float | None] = (100.0,),
        checkpoints: Sequence[str] = (),
        **settings: object,
    ) -> None:
        """Script a run.

        ``planner``, ``implementer`` and ``judge`` are the replies of the n-th turn of that
        role: an agent's JSON, or an answer for a turn that ends another way. The default is
        one valid hypothesis, implemented and approved. ``throughputs`` is what candidates
        measure (None: the benchmark fails), picked by a candidate's commit. ``checkpoints`` answers the n-th
        checkpoint request ``succeeded``, ``failed`` or ``retryable`` (cycling). Any other
        option is a `DynamicConfig` field.
        """
        selected = config(**settings)
        self.config = selected
        self.setup = RunSetup(
            new_launch=lambda: Launch(dynamic, selected),
            harness=harness(selected),
            schemas=dynamic.reply_schemas,
        )
        self._executors = Executors(
            planner=deque([plan_reply(implement("h1"))] if planner is None else planner),
            implementer=deque([implemented()] if implementer is None else implementer),
            judge=deque([reviewed()] if judge is None else judge),
            throughputs=tuple(throughputs),
            ends_jobs_at_submission=True,
            # A turn past the script gets a reply that is no valid JSON, never silence: a
            # silent turn is lost only after its time limit, and a host that dies meanwhile
            # leaves a recovery check core cannot end (the gap named in `vs_core/_waits.py`).
            exhausted="not json",
        )
        self._script = FaultyCheckpoints(self._executors, tuple(checkpoints))

    def headline(self, commit: str) -> float | None:
        """What the candidate of ``commit`` measures (a pure function of its commit)."""
        return self._executors.headline(commit)

    def __call__(self, request: Request, core: CoreState) -> Answer:
        """What the executors report for ``request``."""
        return self._script(request, core)
