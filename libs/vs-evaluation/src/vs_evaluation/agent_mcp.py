"""Thin stdio MCP client for a run's private evaluation service."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, TypedDict, Unpack

from pydantic import BaseModel, ConfigDict

from vs_agent.api import StdioServerDescriptor, ToolServerDescriptor, ToolSpec, serve_stdio
from vs_evaluation.agent_models import (
    MAX_AGENT_AWAIT_S,
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
    StatusCall,
    SubmitCall,
    WaitArgs,
    WaitCall,
)
from vs_evaluation.agent_wire import Offer, SocketClient
from vs_evaluation.profiler_models import (
    AWAIT_CAP_TEXT,
    AwaitProfilerArgs,
    DispatchProfilerArgs,
    NoArgs,
    ProfilerHandleArgs,
)


def evaluation_mcp_descriptor(grant: EvaluationGrant, socket_path: str) -> ToolServerDescriptor:
    """Describe the thin MCP process for a host-issued role capability."""
    return StdioServerDescriptor(
        name="vs-evaluation",
        command="python",
        args=("-m", "vs_evaluation.agent_mcp"),
        env=tuple(
            {
                "VS_EVALUATION_PRINCIPAL": grant.principal_id,
                "VS_EVALUATION_SCOPE": json.dumps(grant.scope_id),
                "VS_EVALUATION_ROLE": grant.role.value,
                "VS_EVALUATION_PROFILER_AVAILABLE": "1" if grant.profiler_available else "0",
                "VS_EVALUATION_RUN_OBSERVER": "1" if grant.run_observer else "0",
                "VS_EVALUATION_SUSPENSION": "1" if grant.evaluation_suspension else "0",
            }.items()
        ),
        runtime_env=tuple(
            {
                "VS_EVALUATION_SOCKET": socket_path,
                "VS_EVALUATION_TOKEN": grant.token,
            }.items()
        ),
    )


def _socket_wait_s(args: AwaitArgs | AwaitProfilerArgs) -> float:
    """Socket deadline for an await: the service's cap on the wait, plus slack."""
    return min(args.timeout_s, MAX_AGENT_AWAIT_S) + 5.0


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
    offer = Offer(SocketClient(socket_path), token)
    tools: list[ToolSpec[Any]] = []
    if evaluation_suspension:
        tools.append(
            offer.tool(
                "validate_evaluation_wait",
                "Validate all evaluation handles before ending with waiting_for_evaluation. "
                "Only this principal's live evaluations or scope-owned host captures may wait; "
                "profiler operation IDs and other principals' evaluations are errors.",
                WaitArgs,
                WaitCall,
            )
        )
    if role is EvaluationAgentRole.JUDGE:
        tools.append(
            offer.tool(
                "evaluation_status",
                "Read a candidate evaluation's current durable state without waiting. "
                "Use this tool and accepted_evidence instead of reading .vibesys/state.",
                HandleArgs,
                StatusCall,
            )
        )
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
                    "Withdraw this agent's evaluation wait; the capture is cancelled only "
                    "after every requester leaves.",
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
