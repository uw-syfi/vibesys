"""A scripted dynamic run that parks, then Resume, reaches another turn or a typed end."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.vibesys.dynamic_world import DynamicWorld

from vs_core.api import (
    ControlId,
    ControlInput,
    DispatchTurn,
    PauseOwner,
    Request,
    ResumeSessionTurn,
    RunControlEvent,
    RunStatus,
    SubmitMeasurement,
)
from vs_project.api import FakeStateStore
from vs_runtime.api.testing import FakePublicationDelivery, HostOptions, scripted_host

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import CoreEvent, CoreState, Transition
    from vs_core.api.testing import Answer


class _ParkOnMeasurement:
    """Answer like the dynamic world, and park once its first measurement is in hand.

    The same world continues after Resume, so job ids and scripted replies do not restart.
    """

    admit: Callable[[CoreEvent, float], Transition] | None = None

    def __init__(self, world: DynamicWorld) -> None:
        self._world = world
        self.parked = False

    def __call__(self, request: Request, core: CoreState) -> Answer:
        admit = self.admit
        if not self.parked and isinstance(request, SubmitMeasurement) and admit is not None:
            self.parked = True
            admit(
                RunControlEvent(
                    control=ControlInput(
                        control_id=ControlId(root="pause"),
                        action="pause",
                        owner=PauseOwner.OPERATOR,
                    ),
                    now_at=core.run.now_at,
                ),
                core.run.now_at,
            )
        return self._world(request, core)


@pytest.mark.regression
def test_a_parked_dynamic_run_resumes_into_another_turn_or_a_typed_end() -> None:
    """Operator Resume of a parked epoch opens fresh and the search moves."""
    store = FakeStateStore()
    delivery = FakePublicationDelivery()
    world = DynamicWorld()
    launch = world.setup.new_launch()
    script = _ParkOnMeasurement(world)
    first = scripted_host(
        launch,
        script,
        world.setup.harness,
        launch.search.reply_schemas,
        store=store,
        delivery=delivery,
    )
    parked = first.run()
    assert script.parked
    assert parked.halted is None
    assert parked.core.run.status is RunStatus.PARKED
    first.shell.release_lease(now_at=first.clock.now())

    resumed = scripted_host(
        launch,
        script,
        world.setup.harness,
        launch.search.reply_schemas,
        HostOptions(resumed=True),
        store=store,
        delivery=delivery,
        clock_at=first.clock.now() + 1,
    )
    outcome = resumed.run()
    assert outcome.halted is None
    epoch = outcome.core.records.recovery.epoch
    turns = [
        record.request
        for record in outcome.core.records.records
        if isinstance(record.request, DispatchTurn | ResumeSessionTurn)
        and record.dispatched_in == epoch
    ]
    result = outcome.core.run.result
    ended = outcome.core.run.status is RunStatus.TERMINAL and result is not None
    assert turns or ended
