"""In-process asynchronous job handles for long ROCprof MCP captures.

The MCP server owns this registry for its lifetime. Submissions return before
the capture finishes; status and bounded await calls observe the same retained
result, including failures, without launching another capture.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from enum import StrEnum
from threading import Event
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

__all__ = ["CaptureJobs"]


class _Status(StrEnum):
    RUNNING = "running"
    CANCELING = "canceling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class _UnknownCaptureHandleError(ValueError):
    @classmethod
    def for_handle(cls, handle: str) -> _UnknownCaptureHandleError:
        """Describe an unknown process-local handle."""
        return cls(f"unknown capture handle: {handle}")


@dataclass
class _Job:
    handle: str
    cancel_event: Event
    task: asyncio.Task[str]
    status: _Status = _Status.RUNNING
    result: str | None = None


class CaptureJobs:
    """Retain asynchronous capture state behind opaque process-local handles."""

    def __init__(self) -> None:
        """Create an empty registry bound to the current MCP event loop."""
        self._jobs: dict[str, _Job] = {}

    def submit(self, run: Callable[[Event], Awaitable[str]]) -> str:
        """Start one capture and return its handle without awaiting GPU work."""
        handle = f"capture-{uuid.uuid4().hex}"
        cancel_event = Event()

        async def execute() -> str:
            job = self._jobs[handle]
            try:
                result = await run(cancel_event)
            except asyncio.CancelledError:
                cancel_event.set()
                job.status = _Status.CANCELED
                job.result = "capture canceled"
                raise
            except Exception as exc:  # noqa: BLE001  # LW-920190; retained MCP job results must preserve any capture-boundary failure for later inspection.
                job.status = _Status.FAILED
                job.result = f"{type(exc).__name__}: {exc}"
            else:
                job.result = result
                if job.status is _Status.CANCELING:
                    job.status = _Status.CANCELED
                else:
                    job.status = _Status.COMPLETED
            return job.result or ""

        task = asyncio.create_task(execute(), name=handle)
        self._jobs[handle] = _Job(handle=handle, cancel_event=cancel_event, task=task)
        return self._render(self._jobs[handle], timed_out=False)

    def status(self, handle: str) -> str:
        """Return retained status and terminal output for one handle."""
        return self._render(self._required(handle), timed_out=False)

    async def await_result(self, handle: str, timeout_s: float) -> str:
        """Wait at most ``timeout_s`` without canceling work on timeout."""
        if timeout_s < 0:
            return self._error(handle, "timeout_s must be non-negative")
        job = self._required(handle)
        if not job.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(job.task), timeout=timeout_s)
            except TimeoutError:
                return self._render(job, timed_out=True)
            except asyncio.CancelledError:
                raise
        return self._render(job, timed_out=False)

    def cancel(self, handle: str) -> str:
        """Request cooperative cancellation and retain the final result."""
        job = self._required(handle)
        if not job.task.done():
            job.status = _Status.CANCELING
            job.cancel_event.set()
        return self._render(job, timed_out=False)

    def _required(self, handle: str) -> _Job:
        try:
            return self._jobs[handle]
        except KeyError as exc:
            raise _UnknownCaptureHandleError.for_handle(handle) from exc

    @staticmethod
    def _render(job: _Job, *, timed_out: bool) -> str:
        return json.dumps(
            {
                "handle": job.handle,
                "status": job.status.value,
                "timed_out": timed_out,
                "result": job.result,
            },
            sort_keys=True,
        )

    @staticmethod
    def _error(handle: str, message: str) -> str:
        return json.dumps(
            {"handle": handle, "status": "error", "timed_out": False, "result": message},
            sort_keys=True,
        )
