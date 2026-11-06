"""The gRPC shell: ``AgentSessionService`` over a Unix socket, in front of a ``SessionHolder``."""

from __future__ import annotations

import os
import shutil
import threading
from concurrent import futures
from typing import TYPE_CHECKING

import grpc

from vs_sidecar.engine import Engine, InvalidCommandError
from vs_sidecar.gen.vibesys.orchestrator.v1 import agent_session_pb2 as sc
from vs_sidecar.gen.vibesys.orchestrator.v1 import agent_session_pb2_grpc as sc_grpc
from vs_sidecar.holder import InvalidRequestError, OtherSessionError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from vs_sidecar.holder import SessionHolder

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


class Servicer(sc_grpc.AgentSessionServiceServicer):
    """``AgentSessionService`` backed by a holder."""

    def __init__(self, holder: SessionHolder, wakeup: Wakeup) -> None:
        """Serve *holder*; followers sleep on *wakeup*."""
        self._holder = holder
        self._wakeup = wakeup

    def _engine(self, context: grpc.ServicerContext) -> Engine:
        engine = self._holder.engine
        if engine is None:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, "no session has been opened")
        return engine

    # The generated base class names its methods in CamelCase, and gRPC calls them by that name.
    # lint-waiver: LW-770004 [N802]; the override must keep the generated name.
    # > Renaming it leaves the RPC unhandled; a generic handler table would replace the generated
    # > service for four methods and lose its typed signatures.
    def Hello(  # noqa: N802
        self, request: sc.SessionHelloRequest, context: grpc.ServicerContext
    ) -> sc.SessionHelloResponse:
        """Say who answers here and whether they can work with the caller."""
        try:
            return self._holder.hello(request)
        except InvalidRequestError as error:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))

    # lint-waiver: LW-770005 [N802]; the override must keep the generated name.
    # > Same as Hello: the generated service dispatches by this exact name.
    def Open(  # noqa: N802
        self, request: sc.OpenSessionRequest, context: grpc.ServicerContext
    ) -> sc.OpenSessionResponse:
        """Start the session; a repeat answers the same and starts nothing."""
        try:
            response = self._holder.open(request)
        except InvalidRequestError as error:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))
        except OtherSessionError as error:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(error))
        self._wakeup.notify()
        return response

    # lint-waiver: LW-770001 [N802]; the override must keep the generated name.
    # > Same as Hello: the generated service dispatches by this exact name.
    def Send(self, request: sc.SendRequest, context: grpc.ServicerContext) -> sc.SendResponse:  # noqa: N802
        """Carry out a command; a repeated effect id answers as the first did."""
        engine = self._engine(context)
        try:
            engine.send(request.command)
        except InvalidCommandError as error:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))
        return sc.SendResponse()

    # lint-waiver: LW-770002 [N802]; the override must keep the generated name.
    # > Same as Hello: the generated service dispatches by this exact name.
    def Follow(  # noqa: N802
        self, request: sc.FollowRequest, context: grpc.ServicerContext
    ) -> Iterator[sc.FollowResponse]:
        """Stream the events after an offset, then new ones, until the session closes."""
        engine = self._engine(context)
        cursor = request.after_offset
        while context.is_active():
            seen = self._wakeup.version
            for event in engine.events_after(cursor):
                yield sc.FollowResponse(event=event)
                cursor = event.offset
                if event.HasField("closed"):
                    return
            self._wakeup.wait(seen, FOLLOW_POLL_SECONDS)


def serve(holder: SessionHolder, wakeup: Wakeup, socket: Path, workers: int = 8) -> grpc.Server:
    """Start serving on the Unix socket *socket* (replacing a stale one). Returns the server."""
    socket.parent.mkdir(parents=True, exist_ok=True)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=workers))
    sc_grpc.add_AgentSessionServiceServicer_to_server(Servicer(holder, wakeup), server)
    _listen_privately(server, socket)
    server.start()
    return server


def _listen_privately(server: grpc.Server, socket: Path) -> None:
    """Listen on *socket*, reachable only by its owner from the moment it has its name.

    The umask belongs to the whole process, so it is left alone, as in orchestrator/privatesocket:
    the socket is bound in a private directory of its own, given its mode there and renamed into
    place, which also replaces what a crash left.
    """
    private = socket.parent / f".b{os.getpid()}"
    shutil.rmtree(private, ignore_errors=True)
    private.mkdir(mode=0o700)
    try:
        bound = private / "s"
        if server.add_insecure_port(f"unix:{bound}") == 0:
            msg = f"could not listen on {socket}"
            raise OSError(msg)
        bound.chmod(0o600)
        bound.replace(socket)
    finally:
        shutil.rmtree(private, ignore_errors=True)
