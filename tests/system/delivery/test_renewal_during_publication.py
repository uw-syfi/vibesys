"""A publication a delivery writes after the host renewed its lease still lands.

A delivery may await while the host renews (a slow journal write, a network hop). Before, the
write was judged at the time the publication started; the renewal had already stamped the store
with a later time, so the store refused the write as stale and the run halted with "publication
host no longer owns the run". The test builds the host the way the production loop does
(`World.runtime`, `CoreRunHost`, `drive_core`), so it reaches the bug through the same API
before and after the fix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.support.skeleton_world import LEASE, open_skeleton_world

from vs_core.api import RunStatus
from vs_runtime.api.core import CoreRunHost, RunLoopConfig, drive_core, start_core
from vs_sim.api.testing import VirtualClock, current_virtual_clock

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.api.core import (
        Publication,
        PublicationAcknowledgement,
        PublicationContext,
        PublicationDelivery,
    )


RENEWED_AFTER_S = 0.5
"""How long after the publication started the delivery's await lets the host renew."""


class RenewsThenWrites:
    """Renews the host's lease once, as a heartbeat during its await would, then writes."""

    def __init__(self, inner: PublicationDelivery, clock: VirtualClock) -> None:
        self._inner = inner
        self._clock = clock
        self.renewed = 0

    async def publish(
        self, publication: Publication, context: PublicationContext
    ) -> PublicationAcknowledgement:
        """Renew at a later time than the publication started, then deliver it."""
        if self.renewed == 0:
            await self._clock.sleep(RENEWED_AFTER_S)
            context.lease.renew(now_at=context.now_at + RENEWED_AFTER_S, lease_duration=LEASE)
            self.renewed += 1
        return await self._inner.publish(publication, context)


async def test_a_publication_written_after_a_renewal_is_judged_at_the_renewal(
    tmp_path: Path,
) -> None:
    clock = current_virtual_clock()
    with open_skeleton_world(tmp_path) as world:
        process = world.runtime()
        delivery = RenewsThenWrites(process.delivery, clock)
        host = CoreRunHost(process.shell, delivery, clock)
        config = RunLoopConfig(host_id="renewing", lease_duration=LEASE, max_dispatches=60)
        start_core(host, config)
        outcome = await drive_core(host, config)
    assert delivery.renewed == 1
    assert outcome.status == RunStatus.TERMINAL
