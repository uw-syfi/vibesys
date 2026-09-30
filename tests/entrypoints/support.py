"""A Fake detached web gateway, shared by the lifecycle and operator tests."""

from __future__ import annotations

import math
from pathlib import Path

from entrypoints.server import GATEWAY_STOP_TIMEOUT_SECONDS
from server.transport.discovery import WebInstanceHold, WebInstanceRecord

INSTANCE_PATH = Path("/project/.vibesys/web-gateway.json")
GATEWAY_PID = 4321
IDLE_DIRECTORY = WebInstanceHold(holders=(), log_locked=False)
POLL_SECONDS = 0.05
BUDGET_POLLS = int(GATEWAY_STOP_TIMEOUT_SECONDS / POLL_SECONDS)
"""Waits the stop budget allows: the last one can still report `STOPPED`."""


def gateway_record(pid: int) -> WebInstanceRecord:
    """Build the record a gateway with ``pid`` would publish."""
    token = "capability" + "-token"
    return WebInstanceRecord(
        pid=pid,
        port=43_211,
        token=token,
        url=f"http://127.0.0.1:43211/?token={token}",
        project_root="/project",
        started_at=1.0,
    )


class FakeDetachedGateway:
    """An in-memory detached gateway, seen the way an unrelated process sees one.

    It models the facts a stop request can read and their real lifetimes. The
    instance record is published while the gateway serves and unlinked as the
    *first* step of teardown, so ``recorded_pid=None`` is the state a
    shutting-down gateway spends most of its teardown in, and
    ``record_after_polls`` models a record that only becomes readable partway
    through the wait. The directory stays in use while the gateway or any
    descendant that inherited its startup log still has a file open there,
    which is what outlives the record: teardown begins on SIGTERM, or at
    construction with ``already_stopping``, and the files then survive
    ``polls_before_release`` further observations.
    ``polls_before_release=None`` models a gateway that ignores the signal.

    ``holder_pids`` and ``log_locked`` are independent on purpose, because the
    real observations are. ``log_locked=False`` with a holder is a gateway
    started without ``--detach`` (it writes no startup log), ``log_locked=None``
    is a directory whose lock state cannot be read, ``holder_pids=None`` is a
    host that does not expose per-process descriptors, and ``holder_pids`` that
    does not contain ``recorded_pid`` is a record left behind by a killed
    gateway, naming a pid the kernel has since reused.

    The clock advances only when the caller sleeps, so the budget is simulated
    and never waited for.
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-994220 [PLR0913]; each argument is one independently reachable gateway state, and collapsing them into a state object would hide the modelled lifetimes behind a second abstraction every test would then have to build
        self,
        *,
        recorded_pid: int | None = GATEWAY_PID,
        record_after_polls: int = 0,
        holder_pids: tuple[int, ...] | None = (GATEWAY_PID,),
        log_locked: bool | None = True,
        polls_before_release: int | None = 0,
        already_stopping: bool = False,
        signal_error: Exception | None = None,
    ) -> None:
        """Place the gateway in one of the states a stop request can encounter."""
        self.recorded_pid = recorded_pid
        self.record_after_polls = record_after_polls
        self.holder_pids = holder_pids
        self.log_locked = log_locked
        self.polls_before_release = polls_before_release
        self.stopping = already_stopping
        self.signal_error = signal_error
        self.signals: list[int] = []
        self.observations = 0
        self.teardown_polls = 0
        self.last_hold = WebInstanceHold(holders=holder_pids, log_locked=log_locked)
        self.sleeps: list[float] = []

    def read_record(self, instance_path: Path) -> WebInstanceRecord | None:
        """Return the published record, if this gateway has one right now."""
        assert instance_path == INSTANCE_PATH
        if self.recorded_pid is None or self.observations <= self.record_after_polls:
            return None
        return gateway_record(self.recorded_pid)

    def observe(self, instance_path: Path) -> WebInstanceHold:
        """Report who is using the instance directory, advancing teardown by one poll."""
        assert instance_path == INSTANCE_PATH
        self.observations += 1
        self.last_hold = self._hold()
        return self.last_hold

    def _hold(self) -> WebInstanceHold:
        if self.stopping and self.polls_before_release is not None:
            if self.teardown_polls >= self.polls_before_release:
                return IDLE_DIRECTORY
            self.teardown_polls += 1
        return WebInstanceHold(holders=self.holder_pids, log_locked=self.log_locked)

    def terminate(self, pid: int) -> None:
        """Start ordered teardown, unless this gateway cannot be signalled at all."""
        if self.signal_error is not None:
            raise self.signal_error
        self.signals.append(pid)
        self.stopping = True

    def monotonic(self) -> float:
        """Return the simulated clock, which only the caller's waits advance.

        The reading is the correctly-rounded total of the waits, not a running
        sum of them. A real clock does not accumulate one rounding error per
        wait, and a model that did would drift past the budget a fraction of a
        wait early, which is exactly the difference the budget's boundary
        comparison turns on.
        """
        return math.fsum(self.sleeps)

    def sleep(self, seconds: float) -> None:
        """Record a wait, which is the only thing that advances the clock."""
        self.sleeps.append(seconds)
