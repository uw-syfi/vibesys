"""Thin stdio MCP client for a run's private evaluation service."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, Unpack

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from vs_agent.api import ToolServerDescriptor, ToolSpec, expose_as_tools, serve_stdio
from vs_evaluation.agent_models import (
    MAX_AGENT_AWAIT_S,
    AgentEvaluationReply,
    AvailabilityCall,
    AwaitArgs,
    AwaitCall,
    AwaitProfilerCall,
    CancelCall,
    CancelProfilerCall,
    DispatchProfilerCall,
    EvaluationAgentRole,
    EvaluationGrant,
    EvidenceCall,
    EvidenceKindsArgs,
    HandleArgs,
    ProfilerOperationsCall,
    ProfilerStatusCall,
    RunOperationsCall,
    SocketFailure,
    SocketReply,
    StatusCall,
    SubmitCall,
)
from vs_evaluation.profiler_models import (
    AWAIT_CAP_TEXT,
    AgentToolArgs,
    AwaitProfilerArgs,
    DispatchProfilerArgs,
    NoArgs,
    ProfilerHandleArgs,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_REPLY = TypeAdapter(SocketReply)
_TOOL_REPLY = TypeAdapter(AgentEvaluationReply)
_DEFAULT_TIMEOUT_S = 30.0
_MAX_REPLY_BYTES = 1_048_576


def evaluation_mcp_descriptor(grant: EvaluationGrant, socket_path: str) -> ToolServerDescriptor:
    """Describe the thin MCP process for a host-issued role capability."""
    return expose_as_tools(
        name="vs-evaluation",
        entrypoint_module="vs_evaluation.agent_mcp",
        env={
            "VS_EVALUATION_SOCKET": socket_path,
            "VS_EVALUATION_TOKEN": grant.token,
            "VS_EVALUATION_ROLE": grant.role.value,
            "VS_EVALUATION_PROFILER_AVAILABLE": "1" if grant.profiler_available else "0",
            "VS_EVALUATION_RUN_OBSERVER": "1" if grant.run_observer else "0",
            "VS_EVALUATION_SUSPENSION": "1" if grant.evaluation_suspension else "0",
        },
    )


class EvaluationServiceClientError(RuntimeError):
    """The private evaluation service rejected or truncated a tool call."""

    @classmethod
    def rejected(cls, message: str) -> EvaluationServiceClientError:
        """Build an error carrying the host's typed boundary diagnostic."""
        return cls(message)

    @classmethod
    def unavailable(cls, error: OSError) -> EvaluationServiceClientError:
        """Build the error for a service the client could not reach or that dropped the call."""
        return cls(f"evaluation service unavailable: {type(error).__name__}: {error}")

    @classmethod
    def oversized(cls) -> EvaluationServiceClientError:
        """Build the fixed reply size violation."""
        return cls("evaluation service reply exceeded size limit")

    @classmethod
    def incomplete(cls) -> EvaluationServiceClientError:
        """Build the fixed incomplete-frame violation."""
        return cls("evaluation service closed without a complete reply")

    @classmethod
    def invalid(cls, error: ValidationError) -> EvaluationServiceClientError:
        """Build the error for arguments the wire call model rejects."""
        problems = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or 'arguments'}: {item['msg']}"
            for item in error.errors(include_url=False, include_input=False)
        )
        return cls(f"invalid arguments: {problems}")


class _SocketClient:
    def __init__(self, path: Path) -> None:
        self._path = path

    def call(self, request: BaseModel, *, timeout_s: float = _DEFAULT_TIMEOUT_S) -> str:
        document = request.model_dump_json().encode() + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(timeout_s)
                client.connect(str(self._path))
                client.sendall(document)
                response = _read_line(client)
        except OSError as error:
            # A stopped service, a dropped oversized frame, or a timeout: the
            # agent gets the typed tool error, never a raw socket exception.
            raise EvaluationServiceClientError.unavailable(error) from error
        decoded = _REPLY.validate_json(response)
        if isinstance(decoded, SocketFailure):
            raise EvaluationServiceClientError.rejected(decoded.error)
        reply = _TOOL_REPLY.validate_json(json.dumps(decoded.result))
        return reply.model_dump_json()


def _read_line(client: socket.socket) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = client.recv(65536)
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


def _socket_wait_s(args: AwaitArgs | AwaitProfilerArgs) -> float:
    """Socket deadline for an await: the service's cap on the wait, plus slack."""
    return min(args.timeout_s, MAX_AGENT_AWAIT_S) + 5.0


class _Offer:
    """Build tools whose input schema is the agent-supplied part of a wire call."""

    def __init__(self, client: _SocketClient, token: str) -> None:
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


class _CapabilityArgs(TypedDict, total=False):
    profiler_available: bool
    run_observer: bool
    evaluation_suspension: bool


class _ToolCapabilities(BaseModel):
    """Validated host-issued optional tool capabilities; unknown keys are errors."""

    model_config = ConfigDict(extra="forbid")
    profiler_available: bool = False
    run_observer: bool = False
    evaluation_suspension: bool = False


def build_evaluation_tools(
    *,
    socket_path: Path,
    token: str,
    role: EvaluationAgentRole,
    **capabilities: Unpack[_CapabilityArgs],
) -> tuple[ToolSpec[Any], ...]:
    """Build only tools granted by the role and validated host capabilities."""
    policy = _ToolCapabilities.model_validate(capabilities)
    profiler_available = policy.profiler_available
    run_observer = policy.run_observer
    evaluation_suspension = policy.evaluation_suspension
    offer = _Offer(_SocketClient(socket_path), token)
    tools: list[ToolSpec[Any]] = []
    if run_observer:
        tools.append(
            offer.tool(
                "trusted_operations",
                "List recent host-owned evaluation and profiler operations across the run, "
                "including hypothesis principal, candidate identity, lifecycle, original "
                "profiler request, whether every stage recorded trusted evidence, and "
                "each recorded stage's outcome (passed, failed, or observed) with its "
                "metrics. Recorded evidence is not a pass: read the stage outcomes.",
                NoArgs,
                RunOperationsCall,
            )
        )
    if role in {
        EvaluationAgentRole.IMPLEMENTER,
        EvaluationAgentRole.PROFILER,
        EvaluationAgentRole.ORCHESTRATOR,
        EvaluationAgentRole.PORTFOLIO_DISPATCH,
        EvaluationAgentRole.RUN_OBSERVER,
    }:
        tools.append(
            offer.tool(
                "evaluation_availability",
                "Return normalized capacity and queue estimates for semantic evidence, "
                "including observable kinds this role may not submit."
                if role
                in {
                    EvaluationAgentRole.ORCHESTRATOR,
                    EvaluationAgentRole.PORTFOLIO_DISPATCH,
                    EvaluationAgentRole.RUN_OBSERVER,
                }
                or (role is EvaluationAgentRole.IMPLEMENTER and profiler_available)
                else "Return normalized capacity and queue estimates for semantic evidence.",
                EvidenceKindsArgs,
                AvailabilityCall,
            )
        )
    if role in {
        EvaluationAgentRole.IMPLEMENTER,
        EvaluationAgentRole.PROFILER,
        EvaluationAgentRole.ORCHESTRATOR,
    }:
        tools.extend(
            (
                offer.tool(
                    "submit_evaluation",
                    "Submit role-authorized semantic evidence collection without blocking; "
                    "returns an opaque handle, or kind run_stopping when the run is "
                    "stopping or kind scope_released when the orchestrator released this "
                    "workspace's jobs; then nothing was submitted.",
                    EvidenceKindsArgs,
                    SubmitCall,
                ),
                offer.tool(
                    "evaluation_status",
                    "Return the current durable state of an evaluation owned by this agent.",
                    HandleArgs,
                    StatusCall,
                ),
                offer.tool(
                    "await_evaluation",
                    "Wait at most timeout_s for the evaluation to finish. "
                    + AWAIT_CAP_TEXT
                    + " Before it finishes, the call returns a running result with the "
                    "progress recorded so far (state, current stage, finished stages) and "
                    "next_await_s; the evaluation keeps running. Remote evaluations can "
                    "take many minutes: call again with the same handle to keep waiting.",
                    AwaitArgs,
                    AwaitCall,
                    _socket_wait_s,
                ),
                offer.tool(
                    "cancel_evaluation",
                    "Request cancellation of an evaluation owned by this agent.",
                    HandleArgs,
                    CancelCall,
                ),
            )
        )
    if role is EvaluationAgentRole.IMPLEMENTER and profiler_available:
        tools.extend(
            (
                offer.tool(
                    "profiler_operations",
                    "List this logical implementer's recent durable profiler turns, "
                    "including operation and session IDs, original request, exact candidate "
                    "snapshot, and state. Use profiler_status for a turn's full result.",
                    NoArgs,
                    ProfilerOperationsCall,
                ),
                offer.tool(
                    "dispatch_profiler",
                    "Ask a provisioned profiler agent to investigate in natural language. "
                    "Returns session and operation IDs without waiting, or kind "
                    "run_stopping when the run is stopping or kind scope_released when the "
                    "orchestrator released this workspace's jobs; then nothing was dispatched. "
                    "A failed operation "
                    "is terminal; do not repeat an identical request until its candidate, "
                    "provision, or diagnosed failure condition changes.",
                    DispatchProfilerArgs,
                    DispatchProfilerCall,
                ),
                offer.tool(
                    "profiler_status",
                    "Observe one asynchronous profiler-agent turn.",
                    ProfilerHandleArgs,
                    ProfilerStatusCall,
                ),
                offer.tool(
                    "await_profiler",
                    "Wait at most timeout_s for a profiler-agent turn; timeout does not cancel "
                    "it. " + AWAIT_CAP_TEXT,
                    AwaitProfilerArgs,
                    AwaitProfilerCall,
                    _socket_wait_s,
                ),
                offer.tool(
                    "cancel_profiler",
                    "Cancel an obsolete profiler-agent turn.",
                    ProfilerHandleArgs,
                    CancelProfilerCall,
                ),
            )
        )
    if role in {
        EvaluationAgentRole.IMPLEMENTER,
        EvaluationAgentRole.PROFILER,
        EvaluationAgentRole.JUDGE,
        EvaluationAgentRole.ORCHESTRATOR,
    }:
        tools.append(
            offer.tool(
                "accepted_evidence",
                "Read only results accepted by the host trust boundary for this candidate.",
                EvidenceKindsArgs,
                EvidenceCall,
            )
        )
    return tuple(
        tool for tool in tools if not (evaluation_suspension and tool.name == "await_evaluation")
    )


def evaluation_tool_names(
    role: EvaluationAgentRole,
    *,
    profiler_available: bool = False,
    run_observer: bool = False,
    evaluation_suspension: bool = False,
) -> tuple[str, ...]:
    """Return the exact MCP tool surface granted to *role*."""
    inert_token = role.value
    return tuple(
        tool.name
        for tool in build_evaluation_tools(
            socket_path=Path("/unused"),
            token=inert_token,
            role=role,
            profiler_available=profiler_available,
            run_observer=run_observer,
            evaluation_suspension=evaluation_suspension,
        )
    )


def main() -> None:
    """Serve the role-specific tools over stdio from injected grant environment."""
    socket_path = Path(os.environ["VS_EVALUATION_SOCKET"])
    token = os.environ["VS_EVALUATION_TOKEN"]
    role = EvaluationAgentRole(os.environ["VS_EVALUATION_ROLE"])
    profiler_available = os.environ.get("VS_EVALUATION_PROFILER_AVAILABLE") == "1"
    run_observer = os.environ.get("VS_EVALUATION_RUN_OBSERVER") == "1"
    evaluation_suspension = os.environ.get("VS_EVALUATION_SUSPENSION") == "1"
    serve_stdio(
        build_evaluation_tools(
            socket_path=socket_path,
            token=token,
            role=role,
            profiler_available=profiler_available,
            run_observer=run_observer,
            evaluation_suspension=evaluation_suspension,
        ),
        server_name="vs-evaluation",
    )


if __name__ == "__main__":
    main()


__all__ = [
    "build_evaluation_tools",
    "evaluation_mcp_descriptor",
    "evaluation_tool_names",
    "main",
]
