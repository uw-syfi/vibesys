"""Public VibeSys interface for role-limited asynchronous evaluation access."""

from vibesys.evaluation_agent.backend import (
    EvidenceReusingEvaluation,
    SemanticEvaluationBackend,
    SemanticEvaluationExecutor,
    SemanticEvaluationIdentity,
    SemanticEvaluationStage,
)
from vibesys.evaluation_agent.evidence import (
    ArtifactDigest,
    ContentDigest,
    EvidenceFingerprints,
    EvidenceKind,
    EvidenceMetric,
    EvidenceOutcome,
    TrustedEvidence,
)
from vibesys.evaluation_agent.mcp import build_evaluation_tools, evaluation_tool_names
from vibesys.evaluation_agent.models import (
    MAX_AGENT_AWAIT_S,
    AgentEvaluationCall,
    AgentEvaluationReply,
    AvailabilityCall,
    AvailabilityReply,
    AwaitCall,
    AwaitProfilerCall,
    AwaitReply,
    CancelCall,
    CanceledReply,
    CancelProfilerCall,
    DispatchProfilerCall,
    EvaluationAgentRole,
    EvaluationAgentState,
    EvaluationGrant,
    EvaluationOperationObservation,
    EvaluationOperationSnapshot,
    EvidenceCall,
    EvidencePreflightCheck,
    EvidencePreflightDecision,
    EvidencePreflightResolution,
    EvidenceReply,
    ProfilerOperationsCall,
    ProfilerStatusCall,
    RunOperationsCall,
    RunOperationsReply,
    StatusCall,
    StatusReply,
    SubmitCall,
    SubmittedReply,
    SubmittedSemanticEvaluation,
)
from vibesys.evaluation_agent.profiler_models import (
    MAX_PROFILER_NARRATIVE_CHARS,
    MAX_PROFILER_REQUEST_CHARS,
    CompletedProfilerOperation,
    InFlightProfilerOperation,
    ProfilerAgentResult,
    ProfilerAttribution,
    ProfilerAwaitReply,
    ProfilerCanceledReply,
    ProfilerCandidateProjection,
    ProfilerDispatchedReply,
    ProfilerLifecycleEvent,
    ProfilerOperation,
    ProfilerOperationLifecycle,
    ProfilerOperationReference,
    ProfilerOperationResult,
    ProfilerOperationsReply,
    ProfilerOperationState,
    ProfilerResultOutcome,
    ProfilerRunObservation,
    ProfilerStatusReply,
    ProfilerWorkKey,
    ProfilerWorkPurpose,
)
from vibesys.evaluation_agent.profiler_runtime import RuntimeProfilerTurnProvision
from vibesys.evaluation_agent.profiler_service import (
    MAX_LIVE_PROFILER_OPERATIONS,
    PROFILER_TERMINAL_RETENTION,
    ProfilerAgentAccessError,
    ProfilerAgentCapacityError,
    ProfilerAgentService,
    ProfilerAgentServiceHooks,
    ProfilerAgentUnavailableError,
    ProfilerIdempotencyConflictError,
    ProfilerTurnProvision,
)
from vibesys.evaluation_agent.profiler_testing import (
    FakeProfilerTurn,
    FakeProfilerTurnProvision,
)
from vibesys.evaluation_agent.service import (
    EvaluationAgentAccessError,
    EvaluationAgentService,
    EvaluationAgentSocketError,
    EvaluationBackend,
    decide_evidence_preflight,
    submission_evidence_kinds,
)
from vibesys.evaluation_agent.slurm_executor import SlurmSemanticEvaluationExecutor
from vs_agent.api import ToolServerDescriptor, expose_as_tools


def evaluation_mcp_descriptor(grant: EvaluationGrant, socket_path: str) -> ToolServerDescriptor:
    """Describe the thin MCP process for a host-issued role capability."""
    return expose_as_tools(
        name="vibesys-evaluation",
        entrypoint_module="vibesys.evaluation_agent.mcp",
        env={
            "VIBESYS_EVALUATION_SOCKET": socket_path,
            "VIBESYS_EVALUATION_TOKEN": grant.token,
            "VIBESYS_EVALUATION_ROLE": grant.role.value,
            "VIBESYS_PROFILER_AVAILABLE": "1" if grant.profiler_available else "0",
            "VIBESYS_RUN_OBSERVER": "1" if grant.run_observer else "0",
        },
    )


def evaluation_prompt_guidance(
    role: EvaluationAgentRole, *, profiler_available: bool = False
) -> str:
    """Describe the evaluation tools actually granted to an optimization role."""
    names = evaluation_tool_names(role, profiler_available=profiler_available)
    if names == ("evaluation_availability",):
        return (
            "## Evaluation availability\n\n"
            "Query `evaluation_availability` before choosing how many hypotheses depend on "
            "high-cost evaluation. Use its normalized current snapshot with each workstream's "
            "advisory `evaluation_cost`; this role cannot submit or manage evaluations.\n"
        )
    if "submit_evaluation" not in names:
        return (
            "## Trusted evaluation evidence\n\n"
            "Use `accepted_evidence` to read framework-trusted results for this candidate. "
            "Do not invoke evaluator commands directly.\n"
        )
    lifecycle = ", ".join(f"`{name}`" for name in names if name != "accepted_evidence")
    accepted = (
        " Use `accepted_evidence` to read accepted results." if "accepted_evidence" in names else ""
    )
    observation_scope = (
        " Availability reports all evidence kinds, including profile capacity; submission "
        "remains limited to accuracy and benchmark evidence."
        if role is EvaluationAgentRole.ORCHESTRATOR
        or (role is EvaluationAgentRole.IMPLEMENTER and profiler_available)
        else ""
    )
    profiler_loop = (
        " Dispatch profiler work only when the current workstream assignment asks you to "
        "collect delegated profile evidence. Before dispatching, call `profiler_operations` "
        "to recover this "
        "hypothesis's durable turns. Match the original request and exact candidate snapshot. "
        "For a matching turn, use its operation ID with `profiler_status` or a bounded "
        "`await_profiler`; use its session ID with `dispatch_profiler` only for a follow-up "
        "in the same conversation. Evaluation handles belong to the profiler agent and are "
        "not profiler operation IDs. Ask the profiler agent for a concrete diagnostic gap "
        "with `dispatch_profiler`; "
        "label its framework-defined purpose and exact semantic focus so only equivalent work is reused; "
        "omit its session ID to start a conversation or provide it to continue one. Use "
        "`profiler_status`, `await_profiler(timeout_s=...)`, and `cancel_profiler` for its "
        "independent asynchronous lifecycle. Its narrative is advisory; inspect returned "
        "trusted evidence directly or use `accepted_evidence` with the profile kind. A "
        "failed profiler operation is terminal; do not immediately repeat the same request "
        "unless the candidate, provision, or diagnosed failure condition changed."
        if role is EvaluationAgentRole.IMPLEMENTER and profiler_available
        else ""
    )
    implementer_loop = (
        " When the active hypothesis needs correctness or performance evidence and the "
        "candidate is concrete, submit that candidate-scoped evidence directly before "
        "nomination. Request accuracy and benchmark together when both are needed so the "
        "coordinator can fuse their execution. Continue useful local work after submission, "
        "then use a bounded await and read `accepted_evidence` to react to the trusted result "
        "in this turn when practical."
        f"{profiler_loop} "
        "On timeout, preserve the handle for a later turn; timeout does not cancel the work. "
        "A queued or running request is not obsolete merely because its receipt has not yet "
        "resolved candidate identity; do not cancel or duplicate it for that reason. "
        "If the candidate is ready and only that framework evaluation remains, nominate it "
        "with an empty `next_step`; awaiting a framework gate is not implementer work. "
        "Make every submitted variant active in its candidate snapshot; adding an inactive "
        "selector is not a measured candidate. Do not put a framework evaluation request in "
        "`next_step`."
        if role is EvaluationAgentRole.IMPLEMENTER
        else ""
    )
    return (
        "## Semantic evaluation tools\n\n"
        f"The attached `vibesys-evaluation` server provides {lifecycle}. First query "
        "`evaluation_availability`. `submit_evaluation` returns a handle without blocking; "
        "continue useful work, inspect it with `evaluation_status`, or wait only as long as "
        "needed with `await_evaluation(timeout_s=...)`. Cancel obsolete work with "
        f"`cancel_evaluation`.{accepted}{observation_scope}{implementer_loop} Do not invoke evaluator commands "
        "directly. "
        "A request collects candidate-scoped evidence. Framework policy may require the same "
        "semantic gates later, but consumes an exact accepted result instead of rerunning it. "
        "Once the framework accepts an exact result, it is trusted and reused without "
        "rerunning that evaluation.\n"
    )


__all__ = [
    "MAX_AGENT_AWAIT_S",
    "MAX_LIVE_PROFILER_OPERATIONS",
    "MAX_PROFILER_NARRATIVE_CHARS",
    "MAX_PROFILER_REQUEST_CHARS",
    "PROFILER_TERMINAL_RETENTION",
    "AgentEvaluationCall",
    "AgentEvaluationReply",
    "ArtifactDigest",
    "AvailabilityCall",
    "AvailabilityReply",
    "AwaitCall",
    "AwaitProfilerCall",
    "AwaitReply",
    "CancelCall",
    "CancelProfilerCall",
    "CanceledReply",
    "CompletedProfilerOperation",
    "ContentDigest",
    "DispatchProfilerCall",
    "EvaluationAgentAccessError",
    "EvaluationAgentRole",
    "EvaluationAgentService",
    "EvaluationAgentSocketError",
    "EvaluationAgentState",
    "EvaluationBackend",
    "EvaluationGrant",
    "EvaluationOperationObservation",
    "EvaluationOperationSnapshot",
    "EvidenceCall",
    "EvidenceFingerprints",
    "EvidenceKind",
    "EvidenceMetric",
    "EvidenceOutcome",
    "EvidencePreflightCheck",
    "EvidencePreflightDecision",
    "EvidencePreflightResolution",
    "EvidenceReply",
    "EvidenceReusingEvaluation",
    "FakeProfilerTurn",
    "FakeProfilerTurnProvision",
    "InFlightProfilerOperation",
    "ProfilerAgentAccessError",
    "ProfilerAgentCapacityError",
    "ProfilerAgentResult",
    "ProfilerAgentService",
    "ProfilerAgentServiceHooks",
    "ProfilerAgentUnavailableError",
    "ProfilerAttribution",
    "ProfilerAwaitReply",
    "ProfilerCanceledReply",
    "ProfilerCandidateProjection",
    "ProfilerDispatchedReply",
    "ProfilerIdempotencyConflictError",
    "ProfilerLifecycleEvent",
    "ProfilerOperation",
    "ProfilerOperationLifecycle",
    "ProfilerOperationReference",
    "ProfilerOperationResult",
    "ProfilerOperationState",
    "ProfilerOperationsCall",
    "ProfilerOperationsReply",
    "ProfilerResultOutcome",
    "ProfilerRunObservation",
    "ProfilerStatusCall",
    "ProfilerStatusReply",
    "ProfilerTurnProvision",
    "ProfilerWorkKey",
    "ProfilerWorkPurpose",
    "RunOperationsCall",
    "RunOperationsReply",
    "RuntimeProfilerTurnProvision",
    "SemanticEvaluationBackend",
    "SemanticEvaluationExecutor",
    "SemanticEvaluationIdentity",
    "SemanticEvaluationStage",
    "SlurmSemanticEvaluationExecutor",
    "StatusCall",
    "StatusReply",
    "SubmitCall",
    "SubmittedReply",
    "SubmittedSemanticEvaluation",
    "TrustedEvidence",
    "build_evaluation_tools",
    "decide_evidence_preflight",
    "evaluation_mcp_descriptor",
    "evaluation_prompt_guidance",
    "submission_evidence_kinds",
]
