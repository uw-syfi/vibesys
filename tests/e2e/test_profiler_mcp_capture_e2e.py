"""The rocprof MCP server's capture tool under concurrency, with a real process and real threads.

Guarantees under test (through ``FastMCP.call_tool``, the same entry the MCP client uses):

* While a ``profile_timeline`` capture is in flight, other tool calls are still served
  promptly: the capture runs off the event loop.
* A second ``profile_timeline`` while one is in flight is refused as ``busy: capture ...``,
  naming the holder.
* Cancelling the awaiting call stops the target process, and the worker releases the capture
  slot; nothing outlives the cancellation.

Everything else the server does is covered in-process in ``tests/vibesys/loops/test_profiler_mcp.py``;
the capture lifecycle itself runs on a scripted host in ``tests/vibesys/loops/test_capture_runtime.py``.
These three need a real worker thread blocked on a real child process, so they live in a real tier.
"""

import asyncio
import contextlib
import importlib.util
import os
import select
import sys
import textwrap
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Protocol

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from vs_sim.api import Event, OsThreads
from vs_sim.api.testing import wait_or_fail

_REPO = Path(__file__).resolve().parents[2]
_ROCPROF_FIXTURES = _REPO / "tests" / "vibesys" / "loops" / "fixtures" / "rocprof"


class _McpServer(Protocol):
    async def call_tool(
        self, name: str, arguments: dict[str, object]
    ) -> tuple[Sequence[object], dict[str, object]]: ...


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    parent = str(path.parent)
    inserted = parent not in sys.path
    if inserted:
        sys.path.insert(0, parent)
    try:
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        if inserted:
            sys.path.remove(parent)
    return module


@pytest.fixture(scope="module")
def rocprof_server_mod() -> ModuleType:
    return _load_module(
        "_e2e_rocprof_server", _REPO / "resources" / "profilers" / "rocprof" / "server.py"
    )


async def _call_tool(server: _McpServer, name: str, **kwargs: object) -> str:
    _, structured = await server.call_tool(name, kwargs)
    result = structured["result"]
    if not isinstance(result, str):
        raise TypeError
    return result


_FAKE_ROCPROFV3_TIMELINE_SOURCE = textwrap.dedent(
    f"""
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    argv = sys.argv[1:]
    out_dir = None
    for i, tok in enumerate(argv):
        if tok == "-d" and i + 1 < len(argv):
            out_dir = argv[i + 1]
            break
    wrapped = argv[argv.index("--") + 1 :] if "--" in argv else []
    rc = subprocess.Popen(wrapped).wait() if wrapped else 0
    if rc == 0 and out_dir:
        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        src = Path({str(_ROCPROF_FIXTURES)!r}) / "kernel_trace" / "out_kernel_trace.csv"
        if src.is_file():
            shutil.copy(src, out_path / "out_kernel_trace.csv")
    sys.exit(rc)
    """
)


def _install_fake_rocprofv3_timeline_only(bin_dir: Path) -> Path:
    path = bin_dir / "rocprofv3"
    path.write_text(f"#!{sys.executable}\n{_FAKE_ROCPROFV3_TIMELINE_SOURCE}")
    path.chmod(0o755)
    return path


# -- MCP-level responsiveness / cancellation / busy-guard smoke tests --------
#
# These exercise the async wiring (server.py's `async def profile_timeline`
# + mcp_async.run_cancellable) through FastMCP's own in-memory
# `call_tool`/`list_tools` -- the "in-memory client/server" the pinned
# mcp<2 SDK provides, no stdio subprocess needed (see mcp_async.py's
# docstring). The deep capture_runtime-level coverage for cancel_event/
# exclusive_capture lives in test_capture_runtime.py; this proves the MCP
# tool wrapper actually reaches that machinery, not just capture_runtime in
# isolation.

_SLOW_TARGET_SOURCE = textwrap.dedent(
    """
    import os
    import signal
    import sys

    # Keep the fifo's write end open for the process's whole life: the test
    # observes the target's death as EOF on the read end.
    handle = open(sys.argv[1], "w")
    handle.write(str(os.getpid()))
    handle.flush()
    signal.pause()
    """
)


def _install_slow_target(tmp_path: Path) -> Path:
    path = tmp_path / "slow_target.py"
    path.write_text(_SLOW_TARGET_SOURCE)
    return path


class _PidChannel:
    """A FIFO the slow target reports its pid on and acknowledges teardown.

    The read end is opened (non-blocking) before the target starts, so the
    target's ``open(fifo, "w")`` succeeds immediately, and the event loop is
    woken by the fd becoming readable rather than by a timer.

    A test-only duplicate write end can remain open until the injected worker
    Fake reports that its blocking function returned.  EOF then means both
    the target exited and the worker released its capture slot.
    """

    def __init__(self, path: Path) -> None:
        os.mkfifo(path)
        self.path = path
        self._fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self._teardown_fd: int | None = None
        self._lock = OsThreads().lock()

    def retain_until_worker_returns(self) -> None:
        """Keep EOF from arriving until the injected worker has returned."""
        with self._lock:
            assert self._teardown_fd is None
            self._teardown_fd = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)

    def release_worker_guard(self) -> None:
        """Let EOF acknowledge both target death and worker teardown."""
        with self._lock:
            if self._teardown_fd is not None:
                os.close(self._teardown_fd)
                self._teardown_fd = None

    async def wait_for_start(self, capture_task: asyncio.Future[str]) -> None:
        """Return once the target has reported in, or fail if the capture ends first."""
        loop = asyncio.get_running_loop()
        readable = asyncio.Event()
        loop.add_reader(self._fd, readable.set)
        waiter = asyncio.ensure_future(readable.wait())
        try:
            await asyncio.wait({waiter, capture_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            loop.remove_reader(self._fd)
            waiter.cancel()
        if not readable.is_set():
            diag = f"capture_task.done()={capture_task.done()}"
            if capture_task.done():
                exc = capture_task.exception()
                diag += f" exception={exc!r}"
                if exc is None:
                    diag += f" result={capture_task.result()!r}"
            raise AssertionError(f"target never started: {diag}")  # noqa: TRY003  # LW-910203; this is a boundary error that deliberately embeds the offending value for the operator to act on
        os.read(self._fd, 64)

    def wait_for_target_exit(self, *, timeout_s: float = 30.0) -> bool:
        """True once the target exited and the worker released its EOF guard.

        ``timeout_s`` only bounds the failure case where the process or its
        worker leaked.
        """
        readable, _, _ = select.select([self._fd], [], [], timeout_s)
        return bool(readable) and os.read(self._fd, 64) == b""

    def close(self) -> None:
        self.release_worker_guard()
        os.close(self._fd)


class _WorkerTracker:
    """``run_worker`` seam Fake: runs the real ``run_cancellable``, tracking in-flight workers.

    A cancelled call's worker thread is "abandoned" (anyio's
    ``abandon_on_cancel=True``): it keeps escalating the target and releasing
    the exclusive-capture slot in its own ``finally`` after the coroutine that
    awaited cancellation has already returned.  Once every tracked worker
    returns, ``on_idle`` releases the FIFO's teardown guard, so its EOF is an
    end-to-end completion signal rather than a separate unbounded wait.
    """

    def __init__(
        self, run_cancellable: Callable[..., Awaitable[str]], on_idle: Callable[[], None]
    ) -> None:
        self._run_cancellable = run_cancellable
        self._on_idle = on_idle
        self._in_flight = 0
        self._lock = OsThreads().lock()
        self._started = OsThreads().event()

    def wait_started(self) -> None:
        """Wait for the real cancellable worker to enter its blocking function."""
        wait_or_fail(self._started, "the cancellable worker to start")

    async def __call__(
        self,
        fn: Callable[..., str],
        /,
        *args: object,
        cancel_event: Event,
        **kwargs: object,
    ) -> str:
        with self._lock:
            self._in_flight += 1

        def tracked(*call_args: object, **call_kwargs: object) -> str:
            self._started.set()
            try:
                return fn(*call_args, **call_kwargs)
            finally:
                with self._lock:
                    self._in_flight -= 1
                    if self._in_flight == 0:
                        self._on_idle()

        return await self._run_cancellable(tracked, *args, cancel_event=cancel_event, **kwargs)


class TestRocprofMcpServerCaptureConcurrency:
    def _slow_capture_server(
        self, rocprof_server_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[_McpServer, _WorkerTracker, _PidChannel, str]:
        bin_dir = tmp_path / "fakebin"
        bin_dir.mkdir()
        _install_fake_rocprofv3_timeline_only(bin_dir)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        monkeypatch.setenv("VIBESYS_PROFILE_DIR", str(tmp_path / ".profiles"))
        pid_channel = _PidChannel(tmp_path / "target.pid")
        pid_channel.retain_until_worker_returns()
        slow_script = _install_slow_target(tmp_path)
        tracker = _WorkerTracker(
            rocprof_server_mod.mcp_async.run_cancellable, pid_channel.release_worker_guard
        )
        server = rocprof_server_mod.build_server(run_worker=tracker)
        return server, tracker, pid_channel, f"{sys.executable} {slow_script} {pid_channel.path}"

    def _assert_target_and_slot_released(
        self, rocprof_server_mod: ModuleType, pid_channel: _PidChannel
    ) -> None:
        assert pid_channel.wait_for_target_exit(), (
            "target process or worker leaked past cancellation"
        )
        # The cancelled call's worker thread keeps running (escalating, then
        # releasing the exclusive-capture slot in its own `finally`) after
        # the coroutine that awaited cancellation has already returned.  The
        # FIFO's retained writer makes EOF a causal worker-completion signal,
        # so this cannot race a subsequent capture or hang in a second wait.
        assert rocprof_server_mod.capture_runtime.active_capture() is None, (
            "capture slot still held after cancellation settled"
        )

    def test_profile_timeline_tool_stays_responsive_to_other_calls_while_a_capture_runs(
        self, rocprof_server_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server, tracker, pid_channel, command = self._slow_capture_server(
            rocprof_server_mod, tmp_path, monkeypatch
        )

        async def run() -> None:
            capture_task = asyncio.ensure_future(
                _call_tool(server, "profile_timeline", command=command)
            )
            await pid_channel.wait_for_start(capture_task)
            tracker.wait_started()

            # A cheap call, made while the capture is still in flight, must
            # return promptly instead of waiting behind it on the event
            # loop -- this is the whole point of running the capture off
            # the main loop.
            cheap_out = await asyncio.wait_for(_call_tool(server, "captures"), timeout=3.0)
            assert cheap_out
            assert not capture_task.done()

            capture_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await capture_task

        try:
            asyncio.run(run())
            self._assert_target_and_slot_released(rocprof_server_mod, pid_channel)
        finally:
            pid_channel.close()

    def test_profile_timeline_tool_returns_busy_for_a_second_concurrent_capture(
        self, rocprof_server_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server, tracker, pid_channel, command = self._slow_capture_server(
            rocprof_server_mod, tmp_path, monkeypatch
        )

        async def run() -> str:
            capture_task = asyncio.ensure_future(
                _call_tool(server, "profile_timeline", command=command)
            )
            await pid_channel.wait_for_start(capture_task)
            tracker.wait_started()

            with pytest.raises(ToolError, match="busy: capture") as failed:
                await asyncio.wait_for(
                    _call_tool(server, "profile_timeline", command="true"), timeout=3.0
                )
            busy_out = str(failed.value)

            capture_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await capture_task
            return busy_out

        try:
            busy_out = asyncio.run(run())
            assert "busy: capture " in busy_out
            assert "timeline" in busy_out
            self._assert_target_and_slot_released(rocprof_server_mod, pid_channel)
        finally:
            pid_channel.close()

    def test_profile_timeline_tool_cancellation_stops_the_target_process(
        self, rocprof_server_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server, tracker, pid_channel, command = self._slow_capture_server(
            rocprof_server_mod, tmp_path, monkeypatch
        )

        async def run() -> None:
            task = asyncio.ensure_future(_call_tool(server, "profile_timeline", command=command))
            await pid_channel.wait_for_start(task)
            tracker.wait_started()

            # The target reported that it is waiting for a signal, so the
            # capture task must remain pending until the caller cancels it.
            assert not task.done()
            # Simulates a client-initiated cancellation. Cancelling the
            # awaiting task must not just stop watching -- it must stop the
            # capture.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        try:
            asyncio.run(run())
            self._assert_target_and_slot_released(rocprof_server_mod, pid_channel)
        finally:
            pid_channel.close()
