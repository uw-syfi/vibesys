"""The public API of ``vs_faults``: one fault plan and its boundary wrappers.

Each wrapper implements the interface it wraps and, with no rule for its
boundary, passes every call through unchanged. Production code has no fault
branches; tests and scripts compose these wrappers at the seams a run already
injects (the agent client factory, the connector command, the tool dispatch).
"""

from vs_faults.agent import AgentCrashError, FaultyAgentClient, generated_replies
from vs_faults.connector import classify, connector_command, injected_faults
from vs_faults.plan import (
    AgentFault,
    Boundary,
    ClusterFault,
    ClusterOperation,
    FaultPlan,
    FaultRule,
    ToolFault,
)
from vs_faults.replies import ReplyGenerator, prompt_vocabulary
from vs_faults.tools import FaultyToolDispatch, ToolCallFailedError

__all__ = [
    "AgentCrashError",
    "AgentFault",
    "Boundary",
    "ClusterFault",
    "ClusterOperation",
    "FaultPlan",
    "FaultRule",
    "FaultyAgentClient",
    "FaultyToolDispatch",
    "ReplyGenerator",
    "ToolCallFailedError",
    "ToolFault",
    "classify",
    "connector_command",
    "generated_replies",
    "injected_faults",
    "prompt_vocabulary",
]
