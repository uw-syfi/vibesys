"""The dynamic search wired to the production shell: one whole run simulated by vs-dst.

The flow stories pin each decision and `tests/system` faults every request kind with the
skeleton search, so this run shows only what the wiring adds: a clean run ends live and
succeeds with a retained candidate; it executes no request twice and repeats none; every turn
it asks is given the prompt a render operation made for it and is bounded by the run deadline.

Budget: one whole run (about 1 CPU-s) through the real core and shell; it is the only way to
see the render-before-turn and deadline wiring.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import TYPE_CHECKING

from tests.support.dynamic_script import REGISTRY
from tests.vibesys.dynamic_world import DynamicWorld
from tests.vibesys.orchestration.dynamic._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)

from vibesys.orchestration.dynamic.search.api import RenderRoleArtifacts
from vs_core.api import DispatchTurn, ExecuteRegisteredOperation, ResumeSessionTurn
from vs_core.testing.liveness import Journal, spin_violations
from vs_dst.api import simulate

if TYPE_CHECKING:
    from vs_core.api import CoreState, Request
    from vs_core.testing.drive import Answer

WORKSTREAMS = 2
DEADLINE_AT = 1800.0


class CountingWorld(DynamicWorld):
    """Two approved workstreams under a run deadline; counts how often each request executes."""

    def __init__(self) -> None:
        """Script a planner that starts two workstreams, each implemented and approved."""
        super().__init__(
            planner=[plan_reply(*(implement(f"h{n}") for n in range(WORKSTREAMS)))],
            implementer=[implemented() for _ in range(WORKSTREAMS)],
            judge=[reviewed() for _ in range(WORKSTREAMS)],
            max_in_flight=WORKSTREAMS,
        )
        self.setup = replace(
            self.setup,
            harness=replace(self.setup.harness, deadline_at=DEADLINE_AT),
        )
        self.executed: Counter[str] = Counter()

    def __call__(self, request: Request, core: CoreState) -> Answer:
        """The base world's answer, once counted."""
        assert request.request_id is not None
        self.executed[request.request_id.root] += 1
        return super().__call__(request, core)


def test_a_clean_run_ends_live_and_asks_each_turn_a_rendered_prompt_within_the_deadline() -> None:
    world = CountingWorld()

    run = simulate(world)

    assert run.halted is None, run.halted
    assert run.finished
    assert not run.liveness_violations
    assert max(world.executed.values()) == 1
    assert spin_violations(Journal.from_ledger(run.core)) == []
    result = run.core.run.result
    assert result is not None
    assert result.outcome == "success", result.reason
    assert result.selection is not None
    assert result.selection.kind == "retained_candidate"

    registry = REGISTRY
    records = run.core.records.records
    renders = {
        f"prompt:{record.request.operation_id.root}"
        for record in records
        if isinstance(record.request, ExecuteRegisteredOperation)
        and isinstance(registry.decode(record.request.operation), RenderRoleArtifacts)
    }
    dispatched = [
        record for record in records if isinstance(record.request, DispatchTurn | ResumeSessionTurn)
    ]
    assert dispatched
    for record in dispatched:
        turn = record.request.turn  # ty: ignore[unresolved-attribute]
        assert turn.prompts
        assert {item.artifact_id.root for item in turn.prompts} <= renders
        assert record.until.at <= DEADLINE_AT
    roles = {record.request.turn.session.role_id.root for record in dispatched}  # ty: ignore[unresolved-attribute]
    assert roles >= {"dynamic-orchestrator", "dynamic-implementer"}
