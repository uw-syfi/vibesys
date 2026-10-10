"""The plan's fault names are the ones the agent-side wrappers act on."""

from __future__ import annotations

from typing import get_args

from vs_agent.api.testing import ConversationFaultKind, ProcessFaultKind
from vs_faults.api import ConversationFault, ProcessFault


def test_process_fault_names_match_the_wrapper_kinds() -> None:
    assert {fault.value for fault in ProcessFault} == set(get_args(ProcessFaultKind.__value__))


def test_conversation_fault_names_match_the_wrapper_kinds() -> None:
    assert {fault.value for fault in ConversationFault} == set(
        get_args(ConversationFaultKind.__value__)
    )
