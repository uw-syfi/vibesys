"""The server's committed-view listener fires once per confirmed core commit.

A server tracks a live run through the one committed-state listener on the run
integration. On the core path that hint comes from the runtime's commit observer, so
this drives a real core run (real Git, the Fake Slurm cluster, the production loop) and
checks the listener against the durable store it describes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.support.real_git import NO_GITHUB, REAL_GIT
from tests.support.skeleton_world import drive, open_skeleton_world
from tests.support.workspace_world import RUN_ID

from vibesys.events import CoreEvent, CoreEventType, WinnerSelectedData
from vibesys.run.integration import LocalRunIntegration
from vs_project.api import StoredEnvelope
from vs_runtime.api.core import RuntimeRecord, winner_selection
from vs_runtime.api.testing import idle_environment_system
from vs_sim.api.testing import ManualClock, SimThreads

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

_NAMESPACE = "core-view"
LEASE = 100.0
_DURABLE_CHECKS = 3


async def test_one_core_run_notifies_the_listener_per_commit_and_announces_its_winner_once(
    tmp_path: Path,
) -> None:
    """One call per commit, each told after its record is durable; the winner event follows core.

    Budget: one skeleton run on real Git, the cheapest way to see the production loop's commits.
    """
    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=SimThreads(),
        system=idle_environment_system(),
    )
    heard: list[CoreEvent] = []
    integration.events.subscribe(heard.append)
    with open_skeleton_world(tmp_path) as world:
        store = world.env.project.state_store(RUN_ID)
        seen: list[tuple[str, BaseModel, int]] = []

        def listener(namespace: str, state: BaseModel, changed: tuple[str, ...] | None) -> None:
            assert changed is None
            if len(seen) < _DURABLE_CHECKS:
                # The hint arrives after the commit is durable, never before it. (Loading the
                # store is the cost of the run, so only the first commits are read back.)
                stored = store.load()
                assert isinstance(stored, StoredEnvelope)
                assert stored.revision == len(seen)
            seen.append((namespace, state, len(seen)))

        integration.add_committed_state_listener(listener)
        world.commits = integration.core_commit_observer(RUN_ID, None, _NAMESPACE)
        process = world.runtime()
        process.shell.start("host-a", now_at=0.0, lease_duration=LEASE)
        assert await drive(process, start=1.0) is None

        final = store.load()
        assert isinstance(final, StoredEnvelope)
        # Store revisions count commits from zero; none was skipped or repeated.
        assert final.revision > 0, "the run must commit more than once"
        assert len(seen) == final.revision + 1
        assert {namespace for namespace, _, _ in seen} == {_NAMESPACE}
        assert all(isinstance(state, RuntimeRecord) for _, state, _ in seen)
        assert seen[-1][1] == process.shell.record

        chosen = winner_selection(process.shell.record)
        assert chosen is not None
        announced = [event.data for event in heard if event.type is CoreEventType.WINNER_SELECTED]
        assert announced == [
            WinnerSelectedData(
                selection=chosen.selection,
                revision=chosen.revision,
                settlement_id=chosen.settlement_id,
            )
        ]
