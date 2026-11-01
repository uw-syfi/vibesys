"""Every record a real run commits reads back as the same record, byte for byte.

A restarted host knows only what the store gives back. The runtime record (core state,
strategy state, pending publications, operation payloads by their registered codecs) is
written by `vs_runtime`, kept on disk by `vs_project`'s state store, and decoded again by the
next host. Here every commit of a skeleton run, straight through and across a restart, is
loaded back from disk the moment it is durable: it decodes to the record the host committed,
and encoding the decoded record gives the stored bytes again.

A skeleton run reaches every kind of record core keeps (attempts, sessions and their turns,
owned jobs, settlement, adoption, recovery barriers).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from tests.support.skeleton_strategy import SkeletonStrategy
from tests.support.skeleton_world import CrashPoint, drive, open_skeleton_world, run_until_crash
from tests.support.workspace_world import RUN_ID

from vs_core.api import FlowState, RunStatus
from vs_project.api import StoredEnvelope
from vs_runtime.api.core import RuntimeRecord

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.skeleton_world import Process, World

    from vs_project.api import StateStore, StoreRecord

_LEASE = 100.0


@dataclass
class _Reloads:
    """Each committed record, beside what the store gives back right after the commit."""

    store: StateStore
    seen: list[tuple[RuntimeRecord[Any], StoreRecord | None]] = field(default_factory=list)

    def committed(self, previous: RuntimeRecord[Any] | None, current: RuntimeRecord[Any]) -> None:
        """Load the store now; the comparison runs after the host, which must not raise here."""
        del previous
        self.seen.append((current, self.store.load()))


def _start(world: World, host: str, now: float) -> Process:
    process = world.runtime()
    process.shell.start(host, now_at=now, lease_duration=_LEASE)
    return process


_STRATEGIES = {
    "measured": SkeletonStrategy,
    "unmeasured": SkeletonStrategy.unmeasured,
    "discarded": lambda: SkeletonStrategy.unmeasured(keeps_candidate=False),
    "cancelled": SkeletonStrategy.cancelled,
}


# A pull request plays the first case (see `sim_smoke_slow`): a cancelled attempt that restarts
# from disk, about 2 s, where the measured search played straight takes about 5 s.
_CASES = [
    pytest.param(name, crash, id=f"{name}-{'straight' if crash is None else 'restart'}")
    for name in ("cancelled", "unmeasured", "discarded", "measured")
    for crash in (CrashPoint.AFTER_OBSERVATION, None)
]


# The nightly job plays every search both ways. A pull request plays one case of it.
@pytest.mark.slow
@pytest.mark.parametrize(("strategy", "crash"), _CASES)
async def test_every_committed_record_reads_back_as_itself(
    tmp_path: Path, strategy: str, crash: CrashPoint | None
) -> None:
    with open_skeleton_world(tmp_path, _STRATEGIES[strategy]()) as world:
        reloads = _Reloads(world.env.project.state_store(RUN_ID))
        world.commits = reloads
        process = _start(world, "host-a", 0.0)
        now = 1.0
        if crash is not None:
            now = await run_until_crash(process, crash, start=now)
            process = _start(world, "host-b", now + _LEASE + 1.0)
            now += _LEASE + 2.0
        assert await drive(process, start=now) is None
        assert process.shell.record.envelope.core.run.status == RunStatus.TERMINAL
        registry = world.bindings().registry
    assert len(reloads.seen) > 10
    for committed, stored in reloads.seen:
        assert isinstance(stored, StoredEnvelope)
        decoded = RuntimeRecord[FlowState].decode(stored, registry)
        assert decoded == committed
        assert decoded.model_dump_json().encode() == stored.payload
