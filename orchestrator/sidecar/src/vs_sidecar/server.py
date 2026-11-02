"""The gRPC shell: ``SidecarService`` over a Unix socket, in front of an ``Engine``."""

from __future__ import annotations

import threading
from concurrent import futures
from typing import TYPE_CHECKING

import grpc

from vs_sidecar.engine import InvalidCommandError
from vs_sidecar.gen.vibesys.orchestrator.v1 import sidecar_pb2 as sc
from vs_sidecar.gen.vibesys.orchestrator.v1 import sidecar_pb2_grpc as sc_grpc

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from vs_sidecar.engine import Engine

#: How long a follower waits for news before it checks that its caller is still there.
FOLLOW_POLL_SECONDS = 1.0


class Wakeup:
    """Wakes followers and the worker when the engine has news."""

    def __init__(self) -> None:
        """Start with nothing to report."""
        self._condition = threading.Condition()
        self._version = 0

    def notify(self) -> None:
        """Say that something changed."""
        with self._condition:
            self._version += 1
            self._condition.notify_all()

    def wait(self, seen: int, timeout: float) -> int:
        """Block until the version moves past *seen* or *timeout* passes; returns the version."""
        with self._condition:
            self._condition.wait_for(lambda: self._version != seen, timeout)
            return self._version

    @property
    def version(self) -> int:
        """The current version."""
        with self._condition:
            return self._version


class Servicer(sc_grpc.SidecarServiceServicer):
    """``SidecarService`` backed by an engine."""

    def __init__(self, engine: Engine, wakeup: Wakeup) -> None:
        """Serve *engine*; followers sleep on *wakeup*."""
        self._engine = engine
        self._wakeup = wakeup

    # The generated base class names its methods in CamelCase, and gRPC calls them by that name.
    # lint-waiver: LW-770001 [N802]; the override must keep the generated name.
    # > Renaming it leaves the RPC unhandled; a generic handler table would replace the generated
    # > service for two methods and lose its typed signatures.
    def Send(self, request: sc.SendRequest, context: grpc.ServicerContext) -> sc.SendResponse:  # noqa: N802
        """Carry out a command; a repeated effect id answers as the first did."""
        try:
            self._engine.send(request.command)
        except InvalidCommandError as error:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))
        return sc.SendResponse()

    # lint-waiver: LW-770002 [N802]; the override must keep the generated name.
    # > Same as Send: the generated service dispatches by this exact name.
    def Follow(  # noqa: N802
        self, request: sc.FollowRequest, context: grpc.ServicerContext
    ) -> Iterator[sc.FollowResponse]:
        """Stream the events after an offset, then new ones, until the session closes."""
        cursor = request.after_offset
        while context.is_active():
            seen = self._wakeup.version
            for event in self._engine.events_after(cursor):
                yield sc.FollowResponse(event=event)
                cursor = event.offset
                if event.HasField("closed"):
                    return
            self._wakeup.wait(seen, FOLLOW_POLL_SECONDS)


def serve(engine: Engine, wakeup: Wakeup, socket: Path, workers: int = 8) -> grpc.Server:
    """Start serving on the Unix socket *socket* (replacing a stale one). Returns the server."""
    socket.parent.mkdir(parents=True, exist_ok=True)
    socket.unlink(missing_ok=True)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=workers))
    sc_grpc.add_SidecarServiceServicer_to_server(Servicer(engine, wakeup), server)
    if server.add_insecure_port(f"unix:{socket}") == 0:
        msg = f"could not listen on {socket}"
        raise OSError(msg)
    socket.chmod(0o600)
    server.start()
    return server
