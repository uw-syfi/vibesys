"""Run a blocking profiler capture off the FastMCP event loop, cancellably.

FastMCP (pinned ``mcp<2``) dispatches each tool call on the server's single
stdio event loop. A ``profile_*`` tool whose handler is a plain ``def`` runs
its capture -- often minutes long -- synchronously on that loop, blocking
every other in-flight or queued call for the whole duration: a client's
``captures()`` poll, sent while a capture is running, would not get a
response until the capture finishes. Making the tool handler ``async def``
and running its blocking body via ``anyio.to_thread.run_sync`` frees the
event loop for other calls while the capture runs on a worker thread; see
each ``server.py``'s ``profile_*`` tools for the call site.

That alone does not honor client-initiated cancellation. MCP's lowlevel
server runs each request in its own task and, on a client
``notifications/cancelled``, cancels that task -- raising at whichever
``await`` is current (see ``mcp.server.lowlevel.server.Server._handle_request``).
``anyio.to_thread.run_sync``'s default (``abandon_on_cancel=False``) blocks
the *awaiting* coroutine until the worker thread finishes even when its task
is cancelled, so a client that aborts a call would still wait out the whole
capture before seeing a response. ``run_cancellable`` instead runs with
``abandon_on_cancel=True`` and signals a ``threading.Event`` on cancellation,
so:

- the coroutine unwinds (and the MCP layer can send its cancellation
  response) as soon as the cancellation is noticed, not after the capture
  finishes;
- the worker thread -- still running, "abandoned" from anyio's perspective
  -- notices ``cancel_event`` within one poll chunk (see
  ``capture_runtime._wait_with_cancel``) and escalates the target process
  tree through the same SIGTERM/SIGKILL path a timeout uses, so nothing is
  left running on the GPU;
- the in-process capture slot (``capture_runtime.exclusive_capture``) is
  acquired and released *inside* the worker function itself, spanning its
  whole blocking body -- not here -- so it stays held for as long as the
  abandoned thread is still tearing the target down. A concurrent second
  call sees "busy" for that whole window, not just until this coroutine
  returns.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, TypeVar

import anyio.to_thread

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable

__all__ = ["run_cancellable"]

T = TypeVar("T")


async def run_cancellable(
    fn: Callable[..., T],
    /,
    *args: object,
    cancel_event: threading.Event,
    **kwargs: object,
) -> T:
    """Run ``fn(*args, cancel_event=cancel_event, **kwargs)`` on a worker thread.

    ``fn`` must accept a ``cancel_event`` keyword and thread it down to
    ``capture_runtime.run_capture`` (directly, or via a ``capture.py``
    wrapper). If this coroutine's task is cancelled while the call is in
    flight, *cancel_event* is set before the cancellation propagates, so the
    in-flight capture stops promptly instead of running unsupervised to
    completion after the tool call has already returned a cancellation to
    the client. See the module docstring for the full rationale.
    """
    call = functools.partial(fn, *args, cancel_event=cancel_event, **kwargs)
    try:
        return await anyio.to_thread.run_sync(call, abandon_on_cancel=True)
    except anyio.get_cancelled_exc_class():
        cancel_event.set()
        raise
