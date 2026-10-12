"""Commit a hypothesis ledger as the runtime record of a multi run, for read-path tests.

A multi run keeps its state in the run's state store as a core runtime record. The server and the
chat tools read the hypothesis ledger through the registration's projector, so a test of those
read paths commits the ledger the same way, without driving a run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.vibesys.orchestration.multi.strategy.world import MultiWorld

from vibesys.hypothesis import rounds
from vibesys.orchestration.multi.strategy.api import MultiStrategyState
from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    EventCursor,
    HostFence,
    HostId,
    RunEnvelope,
)
from vs_core.testing.drive import new_run
from vs_project.api import StoredEnvelope
from vs_runtime.api.core import RuntimeRecord

if TYPE_CHECKING:
    from vibesys.hypothesis.state import HypothesisState
    from vs_project.api import Project, StoreFence


class SearchRecord:
    """The committed runtime record of one multi run, rewritten with each saved ledger."""

    def __init__(self, project: Project, run_id: str) -> None:
        """Bind the run whose state store receives the records."""
        self._project = project
        self._run_id = run_id
        self._fence: StoreFence | None = None

    def save(self, search: HypothesisState) -> None:
        """Commit a record whose strategy state holds ``search``."""
        encoded = rounds.encode(rounds.Ledger(search))
        state = MultiStrategyState(search_json=encoded.search_json, carry_json=encoded.carry_json)
        setup = MultiWorld().setup
        strategy = setup.new_strategy()
        core = new_run(strategy, setup.harness)
        record = RuntimeRecord[MultiStrategyState].fresh(
            RunEnvelope[MultiStrategyState](
                schema_version=ENVELOPE_SCHEMA_VERSION,
                fence=HostFence(host_id=HostId(root="writer"), epoch=1),
                strategy_id=strategy.declaration.strategy_id,
                state_schema=strategy.declaration.state_schema,
                core=core,
                strategy=state,
                event_cursor=EventCursor(sequence=0),
            )
        )
        store = self._project.state_store(self._run_id)
        if self._fence is None:
            self._fence = store.acquire("writer", now=0, duration=1)
        assert self._fence is not None
        stored = store.load()
        expected = stored.revision if isinstance(stored, StoredEnvelope) else None
        revision = 0 if expected is None else expected + 1
        store.commit(
            expected,
            StoredEnvelope(
                revision=revision, schema_version=1, payload=record.model_dump_json().encode()
            ),
            self._fence,
            now=0,
        )


__all__ = ["SearchRecord"]
