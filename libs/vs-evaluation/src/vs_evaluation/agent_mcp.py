"""Thin stdio MCP client for a run's private evaluation service."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, TypeAdapter

from vs_agent.api import ToolServerDescriptor, ToolSpec, expose_as_tools, serve_stdio
from vs_evaluation.agent_evidence import EvidenceKind
from vs_evaluation.agent_models import (
    MAX_AGENT_AWAIT_S,
    AgentEvaluationReply,
    AvailabilityCall,
    AwaitCall,
    AwaitProfilerCall,
    CancelCall,
    CancelProfilerCall,
    DispatchProfilerCall,
    EvaluationAgentRole,
    EvaluationGrant,
    EvidenceCall,
    ProfilerOperationsCall,
    ProfilerStatusCall,
    RunOperationsCall,
    SocketFailure,
    SocketReply,
    StatusCall,
    SubmitCall,
)
from vs_evaluation.profiler_models import (
    MAX_PROFILER_REQUEST_CHARS,
    ProfilerWorkKey,
)

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
        },
    )


class EvaluationServiceClientError(RuntimeError):
    """The private evaluation service rejected or truncated a tool call."""

    @classmethod
    def rejected(cls, message: str) -> EvaluationServiceClientError:
        """Build an error carrying the host's typed boundary diagnostic."""
        return cls(message)

    @classmethod
    def oversized(cls) -> EvaluationServiceClientError:
        """Build the fixed reply size violation."""
        return cls("evaluation service reply exceeded size limit")

    @classmethod
    def incomplete(cls) -> EvaluationServiceClientError:
        """Build the fixed incomplete-frame violation."""
        return cls("evaluation service closed without a complete reply")


class _Kinds(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_kinds: tuple[EvidenceKind, ...] = Field(
        default=(),
        description="Requested semantic evidence kinds. Empty means every kind granted to this role.",
    )


class _Handle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    handle_id: str = Field(description="Opaque handle returned by submit_evaluation.")


class _Await(_Handle):
    timeout_s: FiniteFloat = Field(
        gt=0,
        le=MAX_AGENT_AWAIT_S,
        description="Maximum seconds to block. Timing out leaves the evaluation running.",
    )


class _DispatchProfiler(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work: ProfilerWorkKey = Field(
        description=(
            "Semantic purpose and exact focus of this work. Reuse occurs only for an exact match."
        )
    )
    request: str = Field(
        min_length=1,
        max_length=MAX_PROFILER_REQUEST_CHARS,
        description="Natural-language profiling or measurement request.",
    )
    session_id: str | None = Field(
        default=None,
        min_length=1,
        description="Omit to start a conversation; provide an earlier session ID to resume it.",
    )
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Optional retry key. Reusing it returns the original operation.",
    )


class _ProfilerOperations(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ProfilerHandle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(description="Opaque operation ID returned by dispatch_profiler.")


class _AwaitProfiler(_ProfilerHandle):
    timeout_s: FiniteFloat = Field(
        gt=0,
        le=MAX_AGENT_AWAIT_S,
        description="Maximum seconds to wait. Timeout leaves the profiler turn running.",
    )


class _SocketClient:
    def __init__(self, path: Path) -> None:
        self._path = path

    def call(self, request: BaseModel, *, timeout_s: float = _DEFAULT_TIMEOUT_S) -> str:
        document = request.model_dump_json().encode() + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout_s)
            client.connect(str(self._path))
            client.sendall(document)
            response = _read_line(client)
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


def build_evaluation_tools(
    *,
    socket_path: Path,
    token: str,
    role: EvaluationAgentRole,
    profiler_available: bool = False,
    run_observer: bool = False,
) -> tuple[ToolSpec[Any], ...]:
    """Build only the tools granted to *role*; the host rechecks every call."""
    client = _SocketClient(socket_path)
    tools: list[ToolSpec[Any]] = []
    if run_observer:
        tools.append(
            ToolSpec(
                name="trusted_operations",
                description=(
                    "List recent host-owned evaluation and profiler operations across the run, "
                    "including hypothesis principal, candidate identity, lifecycle, original "
                    "profiler request, and whether a result crossed the trust boundary."
                ),
                input_schema=_ProfilerOperations,
                handler=lambda _args: client.call(RunOperationsCall(token=token)),
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
            ToolSpec(
                name="evaluation_availability",
                description=(
                    "Return normalized capacity and queue estimates for semantic evidence, "
                    "including observable kinds this role may not submit."
                    if role
                    in {
                        EvaluationAgentRole.ORCHESTRATOR,
                        EvaluationAgentRole.PORTFOLIO_DISPATCH,
                        EvaluationAgentRole.RUN_OBSERVER,
                    }
                    or (role is EvaluationAgentRole.IMPLEMENTER and profiler_available)
                    else "Return normalized capacity and queue estimates for semantic evidence."
                ),
                input_schema=_Kinds,
                handler=lambda args: client.call(
                    AvailabilityCall(token=token, evidence_kinds=args.evidence_kinds)
                ),
            )
        )
    if role in {
        EvaluationAgentRole.IMPLEMENTER,
        EvaluationAgentRole.PROFILER,
        EvaluationAgentRole.ORCHESTRATOR,
    }:
        tools.extend(
            (
                ToolSpec(
                    name="submit_evaluation",
                    description=(
                        "Submit role-authorized semantic evidence collection without blocking; "
                        "returns an opaque handle."
                    ),
                    input_schema=_Kinds,
                    handler=lambda args: client.call(
                        SubmitCall(token=token, evidence_kinds=args.evidence_kinds)
                    ),
                ),
                ToolSpec(
                    name="evaluation_status",
                    description="Return the current durable state of an evaluation owned by this agent.",
                    input_schema=_Handle,
                    handler=lambda args: client.call(
                        StatusCall(token=token, handle_id=args.handle_id)
                    ),
                ),
                ToolSpec(
                    name="await_evaluation",
                    description="Wait at most timeout_s. A timed_out result does not cancel the evaluation.",
                    input_schema=_Await,
                    handler=lambda args: client.call(
                        AwaitCall(
                            token=token,
                            handle_id=args.handle_id,
                            timeout_s=args.timeout_s,
                        ),
                        timeout_s=args.timeout_s + 5.0,
                    ),
                ),
                ToolSpec(
                    name="cancel_evaluation",
                    description="Request cancellation of an evaluation owned by this agent.",
                    input_schema=_Handle,
                    handler=lambda args: client.call(
                        CancelCall(token=token, handle_id=args.handle_id)
                    ),
                ),
            )
        )
    if role is EvaluationAgentRole.IMPLEMENTER and profiler_available:
        tools.extend(
            (
                ToolSpec(
                    name="profiler_operations",
                    description=(
                        "List this logical implementer's recent durable profiler turns, "
                        "including operation and session IDs, original request, exact candidate "
                        "snapshot, and state. Use profiler_status for a turn's full result."
                    ),
                    input_schema=_ProfilerOperations,
                    handler=lambda _args: client.call(ProfilerOperationsCall(token=token)),
                ),
                ToolSpec(
                    name="dispatch_profiler",
                    description=(
                        "Ask a provisioned profiler agent to investigate in natural language. "
                        "Returns session and operation IDs without waiting. A failed operation "
                        "is terminal; do not repeat an identical request until its candidate, "
                        "provision, or diagnosed failure condition changes."
                    ),
                    input_schema=_DispatchProfiler,
                    handler=lambda args: client.call(
                        DispatchProfilerCall(
                            token=token,
                            work=args.work,
                            request=args.request,
                            session_id=args.session_id,
                            idempotency_key=args.idempotency_key,
                        )
                    ),
                ),
                ToolSpec(
                    name="profiler_status",
                    description="Observe one asynchronous profiler-agent turn.",
                    input_schema=_ProfilerHandle,
                    handler=lambda args: client.call(
                        ProfilerStatusCall(token=token, operation_id=args.operation_id)
                    ),
                ),
                ToolSpec(
                    name="await_profiler",
                    description=(
                        "Wait at most timeout_s for a profiler-agent turn; timeout does not cancel it."
                    ),
                    input_schema=_AwaitProfiler,
                    handler=lambda args: client.call(
                        AwaitProfilerCall(
                            token=token,
                            operation_id=args.operation_id,
                            timeout_s=args.timeout_s,
                        ),
                        timeout_s=args.timeout_s + 5.0,
                    ),
                ),
                ToolSpec(
                    name="cancel_profiler",
                    description="Cancel an obsolete profiler-agent turn.",
                    input_schema=_ProfilerHandle,
                    handler=lambda args: client.call(
                        CancelProfilerCall(token=token, operation_id=args.operation_id)
                    ),
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
            ToolSpec(
                name="accepted_evidence",
                description="Read only results accepted by the host trust boundary for this candidate.",
                input_schema=_Kinds,
                handler=lambda args: client.call(
                    EvidenceCall(token=token, evidence_kinds=args.evidence_kinds)
                ),
            )
        )
    return tuple(tools)


def evaluation_tool_names(
    role: EvaluationAgentRole,
    *,
    profiler_available: bool = False,
    run_observer: bool = False,
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
        )
    )


def main() -> None:
    """Serve the role-specific tools over stdio from injected grant environment."""
    socket_path = Path(os.environ["VS_EVALUATION_SOCKET"])
    token = os.environ["VS_EVALUATION_TOKEN"]
    role = EvaluationAgentRole(os.environ["VS_EVALUATION_ROLE"])
    profiler_available = os.environ.get("VS_EVALUATION_PROFILER_AVAILABLE") == "1"
    run_observer = os.environ.get("VS_EVALUATION_RUN_OBSERVER") == "1"
    serve_stdio(
        build_evaluation_tools(
            socket_path=socket_path,
            token=token,
            role=role,
            profiler_available=profiler_available,
            run_observer=run_observer,
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
