"""Explicit effect injection for tests exercising the product run session.

This module keeps fake composition out of the application-facing
``vibesys.api.create_session`` contract. Tests still exercise the same session,
persistence, event, and cleanup lifecycle as product callers.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vibesys.api._session import SessionEffects, _create_session
from vibesys.api.store import (
    RunDocument,
    RunRecordFacts,
    RunRecordReadError,
    WorkspaceChange,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.api.contracts import EventSink, RunView
    from vibesys.api.session import RunSession
    from vibesys.plugin_catalog import OrchestrationRegistry
    from vibesys.run.contracts import RunRequest
    from vs_agent.api import AgentClientProtocol
    from vs_runtime.api.infrastructure import StopTimer
    from vs_sandbox.api import ComputeBackendImpl


@dataclass
class FakeRunRecord:
    """In-memory semantic run record with deterministic history failures."""

    run_id: str
    run_view: RunView
    record_facts: RunRecordFacts
    identity: str = ""
    history: tuple[RunDocument, ...] = ()
    portable: tuple[RunDocument, ...] = ()
    workspace_change_requests: list[tuple[str, str]] = field(default_factory=list, init=False)
    workspace_patch_requests: list[tuple[str, str, tuple[str, ...]]] = field(
        default_factory=list,
        init=False,
    )
    _changes: dict[tuple[str, str], tuple[WorkspaceChange, ...] | RunRecordReadError] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _patches: dict[tuple[str, str, tuple[str, ...]], str | RunRecordReadError] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        """Fill a deterministic cache identity when none was supplied."""
        if not self.identity:
            self.identity = f"fake:{self.run_id}"

    def view(self) -> RunView:
        """Return the configured run projection."""
        return self.run_view

    def facts(self) -> RunRecordFacts:
        """Return the configured manifest facts."""
        return self.record_facts

    def history_documents(self) -> tuple[RunDocument, ...]:
        """Return configured policy-selected history documents."""
        return self.history

    def portable_documents(self) -> tuple[RunDocument, ...]:
        """Return configured portable documents."""
        return self.portable

    def workspace_changes(self, base: str, head: str) -> tuple[WorkspaceChange, ...]:
        """Record and answer one semantic workspace-change request."""
        self.workspace_change_requests.append((base, head))
        result = self._changes.get((base, head), ())
        if isinstance(result, RunRecordReadError):
            raise result
        return result

    def workspace_patch(self, base: str, head: str, paths: tuple[str, ...]) -> str:
        """Record and answer one semantic workspace-patch request."""
        self.workspace_patch_requests.append((base, head, paths))
        result = self._patches.get((base, head, paths), "")
        if isinstance(result, RunRecordReadError):
            raise result
        return result

    def set_workspace_changes(
        self,
        base: str,
        head: str,
        *changes: WorkspaceChange,
    ) -> None:
        """Configure semantic changes for one immutable range."""
        self._changes[(base, head)] = changes

    def set_workspace_patch(
        self,
        base: str,
        head: str,
        paths: tuple[str, ...],
        patch: str,
    ) -> None:
        """Configure patch text for one immutable range and path set."""
        self._patches[(base, head, paths)] = patch

    def fail_workspace_changes(
        self,
        base: str,
        head: str,
        error: RunRecordReadError,
    ) -> None:
        """Configure a typed change-read failure for one range."""
        self._changes[(base, head)] = error

    def fail_workspace_patch(
        self,
        base: str,
        head: str,
        paths: tuple[str, ...],
        error: RunRecordReadError,
    ) -> None:
        """Configure a typed patch-read failure for one range and path set."""
        self._patches[(base, head, paths)] = error


# lint-waiver: LW-122302 [PLR0913]; each test-owned effect stays one keyword,
# > as product callers name them. Taking a SessionEffects bundle instead would
# > change every existing test caller and hide which effect a test replaces.
def create_session(  # noqa: PLR0913
    request: RunRequest,
    *,
    sink: EventSink,
    registry: OrchestrationRegistry,
    agent_client_factory: Callable[..., AgentClientProtocol],
    backend_factory: Callable[..., ComputeBackendImpl],
    stop_timer: StopTimer = asyncio.sleep,
) -> RunSession:
    """Build the product session with caller-owned fake effect factories.

    *stop_timer* times a stop's grace period (see `FakeStopTimer`).
    """
    return _create_session(
        request,
        sink=sink,
        registry=registry,
        effects=SessionEffects(agent_client_factory, backend_factory, stop_timer),
    )


class FakeStopTimer:
    """A stop timer whose waits end only when a test expires them.

    Production times a stop's grace period with ``asyncio.sleep``; this Fake
    records each requested delay and lets the test decide when it has passed,
    from any thread, so no test waits on the wall clock.
    """

    def __init__(self) -> None:
        """Start with no recorded or pending waits."""
        self.delays: list[float] = []
        self._condition = threading.Condition()
        self._pending: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = []

    async def __call__(self, seconds: float) -> None:
        """Record *seconds* and wait until :meth:`expire`."""
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[None] = loop.create_future()
        entry = (loop, waiter)
        with self._condition:
            self.delays.append(seconds)
            self._pending.append(entry)
            self._condition.notify_all()
        try:
            await waiter
        finally:
            with self._condition:
                if entry in self._pending:
                    self._pending.remove(entry)

    def wait_armed(self, timeout: float) -> bool:
        """Block until a wait is pending; *timeout* is a deadlock guard."""
        with self._condition:
            return self._condition.wait_for(lambda: bool(self._pending), timeout)

    def expire(self) -> None:
        """End every pending wait, as if its delay had elapsed."""
        with self._condition:
            pending, self._pending = self._pending, []
        for loop, waiter in pending:
            loop.call_soon_threadsafe(_resolve, waiter)


def _resolve(waiter: asyncio.Future[None]) -> None:
    if not waiter.done():
        waiter.set_result(None)


__all__ = ["FakeRunRecord", "FakeStopTimer", "create_session"]
