"""The public API of ``vs_faults``: one fault plan, its host and cluster boundaries and reply generator.

The wrappers for the agent, its processes and conversations live in ``vs_agent.api.testing``
; the tool-dispatch wrapper stays here because it wraps a plain callable. Each library owns the faults of
the interface it exposes and reads the plan defined here. With no rule for its boundary, every
wrapper passes every call through unchanged. Production code has no fault branches.
"""

from vs_faults.connector import (
    classify,
    connector_command,
    injected_faults,
)
from vs_faults.connector import (
    handle as handle_cluster_request,
)
from vs_faults.connector import (
    handle_with as handle_cluster_request_with,
)
from vs_faults.host import Crossing, FaultGate, HostCrashError
from vs_faults.plan import (
    AgentFault,
    Boundary,
    ClusterFault,
    ClusterOperation,
    ConversationFault,
    FaultPlan,
    FaultRule,
    HostFault,
    ProcessFault,
    ToolFault,
)
from vs_faults.replies import ReplyGenerator, prompt_vocabulary
from vs_faults.tools import FaultyToolDispatch, ToolCallFailedError

__all__ = [
    "AgentFault",
    "Boundary",
    "ClusterFault",
    "ClusterOperation",
    "ConversationFault",
    "Crossing",
    "FaultGate",
    "FaultPlan",
    "FaultRule",
    "FaultyToolDispatch",
    "HostCrashError",
    "HostFault",
    "ProcessFault",
    "ReplyGenerator",
    "ToolCallFailedError",
    "ToolFault",
    "classify",
    "connector_command",
    "handle_cluster_request",
    "handle_cluster_request_with",
    "injected_faults",
    "prompt_vocabulary",
]
