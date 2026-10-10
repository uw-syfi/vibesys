"""The tool-call fault wrapper delivers each fault as declared."""

from __future__ import annotations

import pytest

from vs_faults.api import (
    Boundary,
    FaultPlan,
    FaultRule,
    FaultyToolDispatch,
    ToolCallFailedError,
    ToolFault,
)


@pytest.mark.parametrize("fault", list(ToolFault))
def test_a_tool_fault_delivers_the_call_as_declared(fault: ToolFault) -> None:
    delivered: list[str] = []

    def dispatch(name: str, _arguments: object) -> dict[str, object]:
        delivered.append(name)
        return {"n": len(delivered)}

    rule = FaultRule(boundary=Boundary.TOOL_CALL, target="submit", at=1, fault=fault)
    tools = FaultyToolDispatch(dispatch, FaultPlan(seed=1, rules=(rule,)))

    if fault is ToolFault.DUPLICATE:
        assert tools("submit", {}) == {"n": 2}
    else:
        with pytest.raises(ToolCallFailedError):
            tools("submit", {})
    assert tools("status", {}) == {"n": len(delivered)}
    runs = {ToolFault.ERROR: 0, ToolFault.DROPPED: 0, ToolFault.TIMEOUT: 1, ToolFault.DUPLICATE: 2}
    assert delivered.count("submit") == runs[fault]
