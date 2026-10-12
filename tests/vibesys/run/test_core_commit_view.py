"""The server's committed-view listener fires once per confirmed core commit.

A server tracks a live run through the one committed-state listener on the run
integration. On the core path that hint comes from the runtime's commit observer, so
this drives a real core run (real Git, the Fake Slurm cluster, the production loop) and
checks the listener against the durable store it describes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.support.skeleton_world import drive, open_skeleton_world
from tests.support.workspace_world import RUN_ID

from vibesys.events import CoreEvent, CoreEventType, WinnerSelectedData
from vibesys.run.integration import LocalRunIntegration
from vs_project.api import StoredEnvelope
from vs_runtime.api.core import RuntimeRecord, winner_selection

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

_NAMESPACE = "core-view"
LEASE = 100.0


async def test_listener_fires_after_each_confirmed_commit(tmp_path: Path) -> None:
    """One call per commit, each seeing the durable record it was told about."""
    integration = LocalRunIntegration()
    with open_skeleton_world(tmp_path) as world:
        store = world.env.project.state_store(RUN_ID)
        seen: list[tuple[str, BaseModel, int]] = []

        def listener(namespace: str, state: BaseModel, changed: tuple[str, ...] | None) -> None:
            assert changed is None
            stored = store.load()
            assert isinstance(stored, StoredEnvelope)
            # The hint arrives after the commit is durable, never before it.
            seen.append((namespace, state, stored.revision))

        integration.add_committed_state_listener(listener)
        world.commits = integration.core_commit_observer(RUN_ID, None, _NAMESPACE)
        process = world.runtime()
        process.shell.start("host-a", now_at=0.0, lease_duration=LEASE)
        assert await drive(process, start=1.0) is None

        final = store.load()
        assert isinstance(final, StoredEnvelope)
        # Store revisions count commits from zero; none was skipped or repeated.
        assert final.revision > 0, "the run must commit more than once"
        assert [revision for _, _, revision in seen] == list(range(final.revision + 1))
        assert {namespace for namespace, _, _ in seen} == {_NAMESPACE}
        assert all(isinstance(state, RuntimeRecord) for _, state, _ in seen)
        assert seen[-1][1] == process.shell.record


async def test_winner_selection_is_announced_once_with_the_selection_core_recorded(
    tmp_path: Path,
) -> None:
    """Every strategy proposes through core, so the event follows core's recorded selection."""
    integration = LocalRunIntegration()
    heard: list[CoreEvent] = []
    integration.events.subscribe(heard.append)
    with open_skeleton_world(tmp_path) as world:
        world.commits = integration.core_commit_observer(RUN_ID, None, _NAMESPACE)
        process = world.runtime()
        process.shell.start("host-a", now_at=0.0, lease_duration=LEASE)
        assert await drive(process, start=1.0) is None

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
