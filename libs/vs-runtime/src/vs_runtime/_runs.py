"""Process-local run task ownership and replayable semantic event streams."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable


class RunExecution[Result](Protocol):
    """Execution and cleanup capabilities needed by the generic launcher."""

    def start(self) -> None:
        """Subscribe to execution observations before starting work."""
        ...

    async def await_result(self) -> Result:
        """Execute work and return its terminal outcome."""
        ...

    def stop(self) -> None:
        """Request cooperative termination through the run's control."""
        ...

    def close(self) -> None:
        """Release all remaining owned resources, idempotently."""
        ...


class TaskRunHandle[Event, Result, Session: RunExecution[object]]:
    """An independently executing task with a replayable event stream.

    Subscriber cancellation never cancels execution. The task owns session
    cleanup; completed handles remain usable for event replay and results.
    Forced cancellation is issued at most once, so repeated control requests
    cannot interrupt the execution's cancellation cleanup.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._loop = asyncio.get_running_loop()
        self._history: list[Event] = []
        self._changed = asyncio.Event()
        self._finished = False
        self._task: asyncio.Task[Result] | None = None
        self._session: Session | None = None
        self._execution: asyncio.Task[object] | None = None
        self._cancel_requested = False

    @property
    def session(self) -> Session:
        """Return the bound execution session's additional capabilities."""
        if self._session is None:
            message = "run handle has no execution session"
            raise RuntimeError(message)
        return self._session

    def bind(self, session: Session) -> None:
        """Bind the execution exactly once, before starting it."""
        if self._session is not None:
            message = "run handle already has an execution session"
            raise RuntimeError(message)
        self._session = session

    def publish(self, event: Event) -> None:
        """Capture an observation safely even when execution emits off-loop."""
        self._loop.call_soon_threadsafe(self._append, event)

    def _append(self, event: Event) -> None:
        self._history.append(event)
        self._changed.set()

    def start(self) -> None:
        """Start execution as an owned task, idempotently."""
        if self._task is not None:
            return
        session = self.session
        session.start()
        self._execution = self._loop.create_task(session.await_result())
        self._task = self._loop.create_task(self._execute())
        self._task.add_done_callback(self._settled)

    async def _execute(self) -> Result:
        execution = self._execution
        if execution is None:
            message = "run execution has not been started"
            raise RuntimeError(message)
        try:
            return cast("Result", await asyncio.shield(execution))
        except asyncio.CancelledError:
            # Event-loop shutdown cancels both owned tasks. Shielding prevents
            # the completion task from cancelling execution a second time
            # while execution is already releasing its async resources.
            self.cancel()
            while not execution.done():
                try:
                    await asyncio.shield(execution)
                except asyncio.CancelledError:
                    continue
            raise
        finally:
            self.session.close()

    def _settled(self, task: asyncio.Task[Result]) -> None:
        # Retrieve failure even if nobody awaits result; result still re-raises.
        if not task.cancelled():
            task.exception()
        self._finished = True
        self._changed.set()

    async def events(self) -> AsyncGenerator[Event]:
        """Replay all observations, then follow until execution settles."""
        index = 0
        while True:
            while index < len(self._history):
                event = self._history[index]
                index += 1
                yield event
            if self._finished:
                return
            self._changed.clear()
            await self._changed.wait()

    def stop(self) -> None:
        """Request cooperative termination unless cancellation has begun."""
        if self._cancel_requested or (self._execution is not None and self._execution.cancelling()):
            return
        self.session.stop()

    def cancel(self) -> None:
        """Force cancellation once, leaving subsequent cleanup uninterrupted."""
        execution = self._execution
        if execution is None or self._cancel_requested:
            return
        # Retain intent even if execution suppresses or clears cancellation.
        # Event-loop shutdown may already have cancelled this owned task.
        self._cancel_requested = True
        if not execution.done() and not execution.cancelling():
            execution.cancel()

    async def result(self) -> Result:
        """Await completion independently of subscriber lifetime."""
        if self._task is None:
            message = "run handle has not been started"
            raise RuntimeError(message)
        return await asyncio.shield(self._task)

    @property
    def active(self) -> bool:
        """Whether the execution task has not yet reached a terminal state."""
        return self._task is not None and not self._task.done()


class InProcessRuns[Request, Event, Result, Session: RunExecution[object]]:
    """Generic launch mechanism; the caller supplies all assembly policy.

    Identity is unique within this process, including retained terminal runs.
    ``attach`` retains terminal handles; ``list_active`` lists only executing
    tasks and makes no claim about persisted or remote runs.
    """

    def __init__(
        self,
        session_factory: Callable[[Request, Callable[[Event], None]], Session],
        *,
        identity: Callable[[Request], str],
        is_resume: Callable[[Request], bool],
        prepare: Callable[[Request], Request] | None = None,
    ) -> None:
        self._factory = session_factory
        self._identity = identity
        self._is_resume = is_resume
        self._prepare = prepare
        self._handles: dict[str, TaskRunHandle[Event, Result, Session]] = {}

    def start(self, request: Request) -> TaskRunHandle[Event, Result, Session]:
        """Validate a fresh request and return its independently started task."""
        if self._is_resume(request):
            message = "start requires a fresh request without resume"
            raise ValueError(message)
        return self._launch(request)

    def resume(self, request: Request) -> TaskRunHandle[Event, Result, Session]:
        """Validate a resume request and return its independently started task."""
        if not self._is_resume(request):
            message = "resume requires request.resume"
            raise ValueError(message)
        return self._launch(request)

    def _launch(self, request: Request) -> TaskRunHandle[Event, Result, Session]:
        request = self._prepare(request) if self._prepare is not None else request
        run_id = self._identity(request)
        if not run_id:
            message = "run_id must not be empty"
            raise ValueError(message)
        previous = self._handles.get(run_id)
        if previous is not None and previous.active:
            message = f"run_id is already active: {run_id}"
            raise ValueError(message)
        if previous is not None and not self._is_resume(request):
            message = f"run_id already exists: {run_id}"
            raise ValueError(message)
        handle = self._new_handle(run_id)
        session = self._factory(request, handle.publish)
        handle.bind(session)
        try:
            handle.start()
        except BaseException:
            session.close()
            raise
        # A resumed execution has a new launch position; replacing a dict
        # value alone would retain the previous execution's insertion order.
        self._handles.pop(run_id, None)
        self._handles[run_id] = handle
        return handle

    def _new_handle(self, run_id: str) -> TaskRunHandle[Event, Result, Session]:
        return TaskRunHandle[Event, Result, Session](run_id)

    def attach(self, run_id: str) -> TaskRunHandle[Event, Result, Session]:
        """Return a retained active or terminal handle, or raise KeyError."""
        return self._handles[run_id]

    def list_active(self) -> tuple[TaskRunHandle[Event, Result, Session], ...]:
        """List executing handles owned by this process, in launch order."""
        return tuple(handle for handle in self._handles.values() if handle.active)


class FakeRunHandle[Event, Result, Session: RunExecution[object]](
    TaskRunHandle[Event, Result, Session]
):
    """Faithful in-memory run lifetime over a caller-owned Fake session."""


class FakeRuns[Request, Event, Result, Session: RunExecution[object]](
    InProcessRuns[Request, Event, Result, Session]
):
    """Fake launch registry with exactly the production lifetime validation.

    The caller supplies an in-memory execution session with the same request
    validation as its production execution. Registry, task and event semantics
    share one implementation so cancellation and errors cannot diverge.
    """

    def _new_handle(self, run_id: str) -> FakeRunHandle[Event, Result, Session]:
        return FakeRunHandle[Event, Result, Session](run_id)
