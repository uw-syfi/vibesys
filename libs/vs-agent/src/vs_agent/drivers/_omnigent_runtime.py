"""Driver-owned asynchronous runtime for Omnigent sessions."""

# ruff: noqa: TRY003, TRY301

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Coroutine


def _publish_completion[Result](
    task: asyncio.Task[Result],
    completion: concurrent.futures.Future[Result],
) -> None:
    """Transfer one loop task's terminal result to its thread-safe future."""
    if task.cancelled():
        completion.cancel()
        return
    try:
        completion.set_result(task.result())
    except BaseException as exc:  # noqa: BLE001
        completion.set_exception(exc)


class OmnigentAsyncTask[Result]:
    """Cross-thread handle for one task owned by the Omnigent event loop."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        task: asyncio.Task[Result],
        completion: concurrent.futures.Future[Result],
    ) -> None:
        self._loop = loop
        self._task = task
        self._completion = completion

    def result(self) -> Result:
        """Wait for and return the task's result on the calling thread."""
        return self._completion.result()

    def cancel_and_wait(self) -> None:
        """Cancel the loop task and wait until its coroutine has fully unwound."""
        if self._completion.done():
            return
        cancellation = asyncio.run_coroutine_threadsafe(
            self._cancel_and_wait(),
            self._loop,
        )
        cancellation.result()

    async def _cancel_and_wait(self) -> None:
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)


class OmnigentAsyncRuntime:
    """Own one event loop thread shared by every session in a driver."""

    def __init__(self, *, start_timeout: float) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._state_lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(
            target=self._serve,
            name="vibesys-omnigent-runtime",
            daemon=True,
        )
        started = False
        try:
            self._thread.start()
            started = True
            if not self._ready.wait(timeout=start_timeout):
                raise RuntimeError("Omnigent event loop did not start")
        except BaseException:
            if started:
                with contextlib.suppress(BaseException):
                    self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join()
            if not self._thread.is_alive():
                with contextlib.suppress(BaseException):
                    self._loop.close()
            raise

    def submit[Result](
        self,
        awaitable: Coroutine[Any, Any, Result],
    ) -> concurrent.futures.Future[Result]:
        """Schedule an awaitable while the runtime accepts work."""
        with self._state_lock:
            if self._closed:
                awaitable.close()
                raise RuntimeError("Omnigent async runtime is closed")
            return asyncio.run_coroutine_threadsafe(awaitable, self._loop)

    def start_task[Result](
        self,
        awaitable: Coroutine[Any, Any, Result],
    ) -> OmnigentAsyncTask[Result]:
        """Create a loop-owned task whose cancellation can be drained exactly."""
        if self.is_current_thread():
            awaitable.close()
            raise RuntimeError("Omnigent task cannot be started synchronously on its event loop")
        with self._state_lock:
            if self._closed:
                awaitable.close()
                raise RuntimeError("Omnigent async runtime is closed")
            completion: concurrent.futures.Future[Result] = concurrent.futures.Future()
            spawn = asyncio.run_coroutine_threadsafe(
                self._spawn(awaitable, completion),
                self._loop,
            )
            try:
                task = spawn.result()
            except BaseException:
                awaitable.close()
                raise
        return OmnigentAsyncTask(loop=self._loop, task=task, completion=completion)

    def is_current_thread(self) -> bool:
        """Return whether the caller is executing on the runtime loop."""
        return threading.current_thread() is self._thread

    def close(self) -> None:  # noqa: C901  # cleanup continues after every failure
        """Drain loop-owned facilities, stop the thread, and close the loop."""
        if self.is_current_thread():
            raise RuntimeError("Omnigent async runtime cannot close from its event-loop thread")
        with self._state_lock:
            if self._closed:
                return
            self._closed = True

        first_error: BaseException | None = None
        try:
            cleanup = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
            first_error = cleanup.result()
        except BaseException as exc:  # noqa: BLE001
            first_error = exc
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except BaseException as exc:  # noqa: BLE001
            if first_error is None:
                first_error = exc
        self._thread.join()
        if self._thread.is_alive() and first_error is None:
            first_error = RuntimeError("Omnigent event loop did not stop")
        if not self._thread.is_alive():
            try:
                self._loop.close()
            except BaseException as exc:  # noqa: BLE001
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    async def _shutdown(self) -> BaseException | None:
        first_error: BaseException | None = None
        current = asyncio.current_task()
        pending = [task for task in asyncio.all_tasks() if task is not current]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for cleanup in (
            self._loop.shutdown_asyncgens,
            self._loop.shutdown_default_executor,
        ):
            try:
                await cleanup()
            except BaseException as exc:  # noqa: BLE001
                if first_error is None:
                    first_error = exc
        return first_error

    @staticmethod
    async def _spawn[Result](
        awaitable: Coroutine[Any, Any, Result],
        completion: concurrent.futures.Future[Result],
    ) -> asyncio.Task[Result]:
        task = asyncio.create_task(awaitable)
        task.add_done_callback(lambda finished: _publish_completion(finished, completion))
        return task

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()
