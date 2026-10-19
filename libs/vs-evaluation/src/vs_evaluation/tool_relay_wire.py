"""The tool relay wire: one request line to the host, one reply line back.

An agent's tool server runs where the agent runs, often a container whose interpreter holds
only the standard library, pydantic and mcp and which must not see the run's own state. A tool
that needs the host (a run's read-model, say) therefore runs on the host, behind a
``ToolRelayService`` on a unix socket in the run's private socket directory. The container runs
only the relay's thin MCP server (``tool_relay_mcp``), which asks the service what tools exist
and forwards each call, so the container speaks MCP and the host does the reading.

The service answers three requests, each one JSON line:

- ``ping``: is the service up? It needs no token, and a startup check uses it.
- ``list``: the tools offered, as name, description and JSON schema.
- ``call``: run one tool with arguments; the reply carries its text, or says it failed.

``list`` and ``call`` carry the token the host issued with the tool server's descriptor. A
request without it is refused, so another agent container that shares the socket directory
cannot read what the host serves to this one.
"""

from __future__ import annotations

import contextlib
import hmac
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from vs_decode.api import Unreadable, decode_json
from vs_evaluation.socket_lines import read_line

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from vs_mcp.api import ToolSpec
    from vs_sim.api import Clock, Connection, Listener, Network, Threads

CALL_TIMEOUT_S = 120.0
"""How long the tool server waits for the host's answer to one call."""
REQUEST_READ_S = 10.0
"""How long the host waits for a connected client's request line before hanging up."""
MAX_REQUEST_BYTES = 1_048_576
MAX_REPLY_BYTES = 16_777_216
"""The largest reply line the tool server reads; a tool result past it is refused by the host."""


class RelayCall(BaseModel):
    """One request: the action, the issued token, and for ``call`` the tool and its arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    action: Literal["ping", "list", "call"]
    token: str = ""
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class RelayPong(BaseModel):
    """The service is up."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["pong"] = "pong"


class RelayedTool(BaseModel):
    """One tool the host offers: what an agent sees of it."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str
    description: str
    input_schema: dict[str, Any]


class RelayToolList(BaseModel):
    """The tools the host offers, in the order it offers them."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["tools"] = "tools"
    tools: list[RelayedTool]


class RelayResult(BaseModel):
    """What one tool returned, or why it failed (``is_error``)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["result"] = "result"
    text: str
    is_error: bool = False


class RelayRefusal(BaseModel):
    """The host did not run the request: a bad token, an unknown tool, a malformed call."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["refused"] = "refused"
    reason: str


type RelayReply = Annotated[
    RelayPong | RelayToolList | RelayResult | RelayRefusal, Field(discriminator="kind")
]

RELAY_REPLY: TypeAdapter[RelayPong | RelayToolList | RelayResult | RelayRefusal] = TypeAdapter(
    RelayReply
)
"""Decodes one reply line."""


class RelayUnavailableError(Exception):
    """The host could not be reached, or sent something that is not a reply."""


class RelayRefusedError(Exception):
    """The host refused a request; the message is its reason."""


class ToolRelayClient:
    """Dials the host's relay for each request; a transport failure is ``RelayUnavailableError``."""

    def __init__(
        self,
        path: Path,
        token: str,
        *,
        network: Network,
        clock: Clock,
        timeout_s: float = CALL_TIMEOUT_S,
    ) -> None:
        """Bind the socket path, the token, the network that dials it and the deadline's clock."""
        self._path = path
        self._token = token
        self._network = network
        self._clock = clock
        self._timeout_s = timeout_s

    def ping(self) -> None:
        """Return when the service answers; raise ``RelayUnavailableError`` otherwise."""
        reply = self._ask(RelayCall(action="ping"))
        if not isinstance(reply, RelayPong):
            message = f"the relay answered a ping with {reply.kind!r}"
            raise RelayUnavailableError(message)

    def list_tools(self) -> list[RelayedTool]:
        """The tools the host offers.

        Raises:
            RelayUnavailableError: the host could not be reached or answered garbage.
            RelayRefusedError: the host refused the token.
        """
        reply = self._ask(RelayCall(action="list", token=self._token))
        if isinstance(reply, RelayRefusal):
            raise RelayRefusedError(reply.reason)
        if not isinstance(reply, RelayToolList):
            message = f"the relay answered a tool list with {reply.kind!r}"
            raise RelayUnavailableError(message)
        return reply.tools

    def call(self, tool: str, arguments: dict[str, Any]) -> RelayResult:
        """Run ``tool`` on the host.

        Raises:
            RelayUnavailableError: the host could not be reached or answered garbage.
            RelayRefusedError: the host refused the call.
        """
        reply = self._ask(
            RelayCall(action="call", token=self._token, tool=tool, arguments=arguments)
        )
        if isinstance(reply, RelayRefusal):
            raise RelayRefusedError(reply.reason)
        if not isinstance(reply, RelayResult):
            message = f"the relay answered a call with {reply.kind!r}"
            raise RelayUnavailableError(message)
        return reply

    def _ask(self, call: RelayCall) -> RelayPong | RelayToolList | RelayResult | RelayRefusal:
        deadline = self._clock.now() + self._timeout_s
        try:
            connection = self._network.connect(str(self._path), self._timeout_s)
            try:
                connection.send(call.model_dump_json().encode() + b"\n", self._timeout_s)
                line = read_line(connection, self._clock, deadline, MAX_REPLY_BYTES)
            finally:
                connection.close()
        except OSError as error:
            message = f"tool relay unreachable: {type(error).__name__}: {error}"
            raise RelayUnavailableError(message) from error
        if line is None:
            message = "tool relay closed without a reply"
            raise RelayUnavailableError(message)
        reply = decode_json(line, RELAY_REPLY, source="tool relay reply", max_bytes=MAX_REPLY_BYTES)
        if isinstance(reply, Unreadable):
            message = f"tool relay sent a malformed reply ({len(reply.problems)} problem(s))"
            raise RelayUnavailableError(message)
        return reply


class ToolRelayService:
    """Serves a fixed set of tools on a unix socket, one short request per connection, until closed.

    The caller creates the socket's directory private; the service never creates it. Each
    connection is answered on its own worker thread, and the tool handlers run one at a time
    under a lock, as they did when one subprocess served them, so a handler that reads shared
    state never races another call. A handler that raises is a failed result, never a crash of
    the service.
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-861901 [PLR0913]; the tools, the endpoint, the token and the three effects are independent inputs the caller owns.
        self,
        tools: Sequence[ToolSpec[Any]],
        socket_path: Path,
        *,
        token: str,
        network: Network,
        threads: Threads,
        clock: Clock,
    ) -> None:
        """Bind the tools to serve, where to listen, the token to require and the effects used."""
        if not token:
            message = "a tool relay needs a non-empty token"
            raise ValueError(message)
        names = [spec.name for spec in tools]
        if len(set(names)) != len(names):
            message = f"tool relay tools must have distinct names, got {names}"
            raise ValueError(message)
        self._tools = {spec.name: spec for spec in tools}
        self._offered = [
            RelayedTool(
                name=spec.name,
                description=spec.description,
                input_schema=spec.input_schema.model_json_schema(),
            )
            for spec in tools
        ]
        self._socket_path = socket_path
        self._token = token
        self._network = network
        self._threads = threads
        self._clock = clock
        self._guard = threads.lock()
        self._handlers = threads.lock()
        self._listener: Listener | None = None
        self._open: set[Connection] = set()
        self._closing = False

    @property
    def socket_path(self) -> Path:
        """Where the service listens."""
        return self._socket_path

    def start(self) -> None:
        """Listen, replacing a stale socket a previous process of this run left behind."""
        with contextlib.suppress(FileNotFoundError):
            self._socket_path.unlink()
        listener = self._network.listen(str(self._socket_path))
        self._listener = listener
        self._threads.spawn(lambda: self._accept_loop(listener), name="tool-relay-service")

    def close(self) -> None:
        """Stop accepting, hang up on open calls and remove the socket; safe to repeat."""
        with self._guard:
            self._closing = True
            listener, self._listener = self._listener, None
            open_connections, self._open = self._open, set()
        if listener is not None:
            listener.close()
        for connection in open_connections:
            connection.close()
        with contextlib.suppress(FileNotFoundError):
            self._socket_path.unlink()

    def _accept_loop(self, listener: Listener) -> None:
        while True:
            try:
                connection = listener.accept()
            except OSError:
                return  # the listener was closed
            with self._guard:
                closing = self._closing
                if not closing:
                    self._open.add(connection)
            if closing:
                connection.close()
                return
            self._threads.spawn(lambda c=connection: self._answer(c), name="tool-relay-call")

    def _answer(self, connection: Connection) -> None:
        try:
            line = read_line(
                connection, self._clock, self._clock.now() + REQUEST_READ_S, MAX_REQUEST_BYTES
            )
            if line is None:
                return
            reply = self._reply(line)
            connection.send(reply.model_dump_json().encode() + b"\n", REQUEST_READ_S)
        except OSError:
            pass  # the client went away or never sent a whole request
        finally:
            with self._guard:
                self._open.discard(connection)
            connection.close()

    def _reply(self, line: bytes) -> RelayPong | RelayToolList | RelayResult | RelayRefusal:
        call = decode_json(line, RelayCall, source="tool relay call", max_bytes=MAX_REQUEST_BYTES)
        if isinstance(call, Unreadable):
            return RelayRefusal(reason="malformed relay call")
        if call.action == "ping":
            return RelayPong()
        if not hmac.compare_digest(call.token.encode(), self._token.encode()):
            return RelayRefusal(reason="unknown relay token")
        if call.action == "list":
            return RelayToolList(tools=self._offered)
        return self._call(call)

    def _call(self, call: RelayCall) -> RelayResult | RelayRefusal:
        spec = self._tools.get(call.tool or "")
        if spec is None:
            return RelayRefusal(reason=f"unknown tool {call.tool!r}")
        try:
            arguments = spec.input_schema.model_validate(call.arguments)
        except ValidationError as error:
            return RelayResult(text=f"invalid arguments for {spec.name}: {error}", is_error=True)
        try:
            with self._handlers:
                return RelayResult(text=spec.handler(arguments))
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-861902 [BLE001]; a tool handler may raise anything (a store, a git read); the agent gets the failure as a tool error, as FastMCP gives it, and the service keeps serving.
            return RelayResult(text=f"{type(error).__name__}: {error}", is_error=True)


__all__ = [
    "CALL_TIMEOUT_S",
    "MAX_REPLY_BYTES",
    "RELAY_REPLY",
    "RelayCall",
    "RelayPong",
    "RelayRefusal",
    "RelayRefusedError",
    "RelayResult",
    "RelayToolList",
    "RelayUnavailableError",
    "RelayedTool",
    "ToolRelayClient",
    "ToolRelayService",
]
