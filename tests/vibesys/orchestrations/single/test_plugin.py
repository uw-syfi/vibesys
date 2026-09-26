"""Public behavior of the single-agent orchestration policy slice."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from vibesys.orchestrations.single import PLUGIN
from vibesys.orchestrations.single.models import PaidAttempt, SingleState
from vs_runtime.api import AccuracyReceipt, AgentRole, RunStatus, StructuredResponseError
from vs_runtime.api.testing import FakeRunHost

DESIGNER, IMPLEMENTER = PLUGIN.agents


def _plan(*, updates: tuple[dict[str, str], ...] = ()) -> dict[str, object]:
    return {
        "hypothesis_id": "batch-prefill",
        "hypothesis": "batching prefill removes per-request launch overhead",
        "task": "batch the prefill step",
        "pass_criteria": "throughput improves without an accuracy regression",
        "reasoning": "launch overhead dominates the trace",
        "hypothesis_updates": updates,
    }


def _result(verdict: str, *, feedback: str = "") -> dict[str, object]:
    return {
        "summary": "implemented batched prefill",
        "expected_behavior": "higher steady-state throughput",
        "self_review": "reviewed the diff and ran checks",
        "feedback": feedback,
        "verdict": verdict,
    }


class _Script:
    """Deterministic role response queues over the runtime Fake seam."""

    def __init__(self) -> None:
        self._replies: dict[str, deque[object]] = defaultdict(deque)
        self.calls: list[tuple[str, tuple[str, ...], str]] = []

    def enqueue(self, role: AgentRole, *replies: object) -> None:
        self._replies[role.id].extend(replies)

    def respond(
        self,
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        self.calls.append((role.id, history, message))
        reply = self._replies[role.id].popleft()
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _run(script: _Script, options: BaseModel | None = None) -> tuple[RunStatus, FakeRunHost]:
    async def scenario() -> tuple[RunStatus, FakeRunHost]:
        host = FakeRunHost(
            PLUGIN,
            project_root=Path("/candidate"),
            responder=script.respond,
        )
        status = await PLUGIN.orchestrate(
            host,
            options or PLUGIN.options.model_validate({"objective": "Improve request throughput."}),
        )
        await host.close()
        return status, host

    return asyncio.run(scenario())


def test_plugin_declares_fixed_roles_and_strict_options() -> None:
    assert PLUGIN.id == "single-agent"
    assert PLUGIN.agents == (DESIGNER, IMPLEMENTER)
    assert DESIGNER.system_prompt
    assert IMPLEMENTER.system_prompt
    assert PLUGIN.state is SingleState

    with pytest.raises(ValidationError):
        PLUGIN.options.model_validate({"objective": "speed up", "unknown": True})


def test_single_state_owns_paid_attempt_and_accuracy_resume_data() -> None:
    state = SingleState(
        last_paid_attempt=PaidAttempt(
            round_number=2,
            hypothesis_id="batch-prefill",
            attempt=1,
        ),
        accuracy_receipt=AccuracyReceipt(
            run_id="run-1",
            workspace_id=None,
            revision="candidate-revision",
        ),
    )

    serialized = state.model_dump_json(round_trip=True)
    restored = SingleState.model_validate_json(serialized)

    assert restored == state
    assert restored.search.hypotheses == []


def test_invalid_plan_gets_one_correction_in_the_same_session() -> None:
    script = _Script()
    script.enqueue(
        DESIGNER,
        _plan(updates=({"hypothesis_id": "batch-prefill", "reason": "self-update"},)),
        _plan(),
    )
    script.enqueue(IMPLEMENTER, _result("approve"))

    status, host = _run(script)

    assert status is RunStatus.SUCCEEDED
    designer_calls = [call for call in script.calls if call[0] == DESIGNER.id]
    assert [len(call[1]) for call in designer_calls] == [0, 1]
    assert "rejected" in designer_calls[1][2]
    assert host.logs[0].startswith("designer plan rejected")


def test_failed_review_retries_in_the_same_implementation_session() -> None:
    script = _Script()
    script.enqueue(DESIGNER, _plan())
    script.enqueue(
        IMPLEMENTER,
        _result("revise", feedback="accuracy regressed"),
        _result("approve"),
    )

    status, _host = _run(script)

    assert status is RunStatus.SUCCEEDED
    implementer_calls = [call for call in script.calls if call[0] == IMPLEMENTER.id]
    assert [len(call[1]) for call in implementer_calls] == [0, 1]
    assert "accuracy regressed" in implementer_calls[1][2]


def test_retry_exhaustion_is_a_policy_failure() -> None:
    script = _Script()
    script.enqueue(DESIGNER, _plan())
    script.enqueue(
        IMPLEMENTER,
        _result("revise", feedback="first failure"),
        _result("revise", feedback="second failure"),
    )

    status, host = _run(
        script,
        PLUGIN.options.model_validate(
            {"objective": "Improve request throughput.", "max_retries_per_round": 1}
        ),
    )

    assert status is RunStatus.FAILED
    assert host.logs[-1] == "implementation retry budget exhausted"


def test_second_invalid_plan_propagates_policy_error() -> None:
    script = _Script()
    invalid = _plan(updates=({"hypothesis_id": "batch-prefill", "reason": "self-update"},))
    script.enqueue(DESIGNER, invalid, invalid)

    with pytest.raises(ValueError, match="cannot update itself"):
        _run(script)


def test_structured_response_fallback_and_retry_are_plugin_policy() -> None:
    script = _Script()
    script.enqueue(DESIGNER, StructuredResponseError(DESIGNER.id, PLUGIN.options))
    script.enqueue(
        IMPLEMENTER,
        StructuredResponseError(IMPLEMENTER.id, PLUGIN.options),
        _result("approve"),
    )

    status, host = _run(script)

    assert status is RunStatus.SUCCEEDED
    assert host.logs[:2] == (
        "designer returned no structured plan; using the policy fallback",
        "implementer returned no structured result; applying fallback",
    )
    implementer_calls = [call for call in script.calls if call[0] == IMPLEMENTER.id]
    assert "schema-valid response" in implementer_calls[1][2]
