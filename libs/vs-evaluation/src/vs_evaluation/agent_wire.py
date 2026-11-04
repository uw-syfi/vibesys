"""Client side of the evaluation tool wire: one JSON line out, one typed reply back.

Both MCP tool servers (the legacy role-based one and the core-path one) speak this
wire, so the framing, size limit, typed client errors and the schema-derived tool
builder live here once.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING

from pydantic import BaseModel, TypeAdapter, ValidationError

from vs_decode.api import Unreadable, decode_json
from vs_evaluation.agent_models import (
    RECEIPT_LINE,
    AgentEvaluationReply,
    AgentToolArgs,
    SocketFailure,
    SocketReply,
)
from vs_mcp.api import ToolSpec

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_sim.api import Clock, Connection, Network

_REPLY = TypeAdapter(SocketReply)
_TOOL_REPLY = TypeAdapter(AgentEvaluationReply)
_DEFAULT_TIMEOUT_S = 30.0
_MAX_REPLY_BYTES = 1_048_576


class EvaluationServiceClientError(RuntimeError):
    """The private evaluation service rejected or truncated a tool call."""

    @classmethod
    def rejected(cls, message: str) -> EvaluationServiceClientError:
        """Build an error carrying the host's typed boundary diagnostic."""
        return cls(message)

    @classmethod
    def unavailable(cls, error: OSError) -> EvaluationServiceClientError:
        """Build the error for a service the client could not reach or that dropped the call."""
        text = f"evaluation service unavailable: {type(error).__name__}: {error}"
        if isinstance(error, TimeoutError):
            text += (
                "; the call may still have been accepted. Repeat the same call: a repeated "
                "submit_evaluation for an unchanged candidate returns the handle it already made"
            )
        return cls(text)

    @classmethod
    def oversized(cls) -> EvaluationServiceClientError:
        """Build the fixed reply size violation."""
        return cls("evaluation service reply exceeded size limit")

    @classmethod
    def incomplete(cls) -> EvaluationServiceClientError:
        """Build the fixed incomplete-frame violation."""
        return cls("evaluation service closed without a complete reply")

    @classmethod
    def malformed(cls, reply: Unreadable) -> EvaluationServiceClientError:
        """Build the error for a whole reply line that is not a valid wire reply."""
        return cls(f"evaluation service sent a malformed reply: {len(reply.problems)} problem(s)")

    @classmethod
    def invalid(cls, error: ValidationError) -> EvaluationServiceClientError:
        """Build the error for arguments the wire call model rejects."""
        problems = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or 'arguments'}: {item['msg']}"
            for item in error.errors(include_url=False, include_input=False)
        )
        return cls(f"invalid arguments: {problems}")


class SocketClient:
    """One short-lived unix-socket call per tool invocation."""

    def __init__(self, path: Path, *, network: Network, clock: Clock) -> None:
        """Bind the service's socket path, the network that dials it, and the clock its deadline runs on."""
        self._path = path
        self._network = network
        self._clock = clock

    def call(self, request: BaseModel, *, timeout_s: float = _DEFAULT_TIMEOUT_S) -> str:
        """Send one call; return the typed reply as JSON, or raise the typed client error.

        ``timeout_s`` bounds the whole call, so a service that trickles bytes
        without ever ending its line cannot hold the agent's turn for ever.
        """
        document = request.model_dump_json().encode() + b"\n"
        deadline = self._clock.now() + timeout_s
        try:
            connection = self._network.connect(str(self._path), self._remaining(deadline))
            try:
                connection.send(document, self._remaining(deadline))
                response = _read_line(connection, self._clock, deadline)
                with contextlib.suppress(OSError):
                    # A host that cannot hear the receipt loses nothing but its certainty.
                    connection.send(RECEIPT_LINE, self._remaining(deadline))
            finally:
                connection.close()
        except OSError as error:
            # A stopped service, a dropped oversized frame, or a timeout: the
            # agent gets the typed tool error, never a raw socket exception.
            raise EvaluationServiceClientError.unavailable(error) from error
        decoded = decode_json(
            response, _REPLY, source="evaluation service reply", max_bytes=_MAX_REPLY_BYTES
        )
        if isinstance(decoded, SocketFailure):
            raise EvaluationServiceClientError.rejected(decoded.error)
        reply = (
            decoded
            if isinstance(decoded, Unreadable)
            else decode_json(
                json.dumps(decoded.result),
                _TOOL_REPLY,
                source="evaluation service reply result",
                max_bytes=_MAX_REPLY_BYTES,
            )
        )
        if isinstance(reply, Unreadable):
            # A version-skewed or broken service: still the typed tool error.
            raise EvaluationServiceClientError.malformed(reply)
        return reply.model_dump_json()

    def _remaining(self, deadline: float) -> float:
        """Seconds left before ``deadline``; ``TimeoutError`` once none are."""
        remaining = deadline - self._clock.now()
        if remaining <= 0:
            message = "no complete reply before the call's deadline"
            raise TimeoutError(message)
        return remaining


def _read_line(connection: Connection, clock: Clock, deadline: float) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        remaining = deadline - clock.now()
        if remaining <= 0:
            message = "no complete reply before the call's deadline"
            raise TimeoutError(message)
        chunk = connection.recv(65536, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > _MAX_REPLY_BYTES:
            raise EvaluationServiceClientError.oversized()
        if b"\n" in chunk:
            break
    data = b"".join(chunks)
    line, separator, _rest = data.partition(b"\n")
    if not separator:
        raise EvaluationServiceClientError.incomplete()
    return line


class Offer:
    """Build tools whose input schema is the agent-supplied part of a wire call."""

    def __init__(self, client: SocketClient, token: str) -> None:
        """Bind the client and the host-held token every offered call carries."""
        self._client = client
        self._token = token

    def tool[A: AgentToolArgs](
        self,
        name: str,
        description: str,
        args: type[A],
        call: type[A],
        socket_wait_s: Callable[[A], float] | None = None,
    ) -> ToolSpec[A]:
        """Offer *call*'s agent-supplied fields, *args*, as the tool *name*.

        *call* subclasses *args* and adds only the host-held ``action`` and
        ``token``, so the schema the agent is offered and the model the
        service validates are one definition. A value the wire model still
        rejects is a typed tool error.
        """
        if not issubclass(call, args):
            message = f"{call.__name__} does not extend the tool arguments {args.__name__}"
            raise TypeError(message)
        client, token = self._client, self._token

        def handler(values: A) -> str:
            try:
                request = call.model_validate({**values.model_dump(), "token": token})
            except ValidationError as error:
                raise EvaluationServiceClientError.invalid(error) from error
            if socket_wait_s is None:
                return client.call(request)
            return client.call(request, timeout_s=socket_wait_s(values))

        return ToolSpec(name=name, description=description, input_schema=args, handler=handler)


__all__ = ["EvaluationServiceClientError", "Offer", "SocketClient"]
