"""``AgentClient`` hands a provider capacity limit to the installed gate."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api import AgentClient, AgentQuotaError, AgentTurnRequest, QuotaCondition
from vs_agent.api.testing import FakeProvider, FakeTurnScript


def _quota(detail: str = "limit") -> AgentQuotaError:
    return AgentQuotaError("claude", QuotaCondition.QUOTA_EXHAUSTED, detail)


@dataclass
class _Gate:
    """A gate that returns at once, or raises ``refusal``, recording each call."""

    refusal: Exception | None = None
    calls: list[tuple[AgentQuotaError, str, str | None, int]] = field(default_factory=list)

    def wait_for_capacity(
        self, error: AgentQuotaError, turn: AgentTurnRequest, *, role: str, attempt: int
    ) -> None:
        self.calls.append((error, role, turn.invocation_id, attempt))
        if self.refusal is not None:
            raise self.refusal


def _invoke(client: AgentClient, workspace: Path) -> str:
    return client.invoke_text(
        kind="implementer",
        workspace=workspace,
        system_prompt="sys",
        user_prompt="go",
        round_label="r1",
        invocation_id="inv-1",
    )


def _client(answers: tuple[AgentQuotaError | str, ...]) -> AgentClient:
    driver = FakeProvider(script=FakeTurnScript(answers=answers))
    return AgentClient(driver, provider="mock", model_name="m")


def test_without_a_gate_the_quota_error_reaches_the_caller(tmp_path: Path) -> None:
    client = _client((_quota(), "ok"))

    with pytest.raises(AgentQuotaError):
        _invoke(client, tmp_path)


@given(stops=st.integers(min_value=1, max_value=5))
def test_the_turn_is_sent_again_after_each_gate_return(stops: int) -> None:
    gate = _Gate()
    client = _client((*[_quota(f"stop {n}") for n in range(stops)], "ok"))
    client.set_capacity_gate(gate)

    with TemporaryDirectory() as tmp:
        text = _invoke(client, Path(tmp))

    assert text == "ok"
    assert [(error.detail, role, inv, attempt) for error, role, inv, attempt in gate.calls] == [
        (f"stop {n}", "implementer", "inv-1", n + 1) for n in range(stops)
    ]


def test_a_gate_that_raises_ends_the_turn_with_its_error(tmp_path: Path) -> None:
    gate = _Gate(refusal=RuntimeError("give up"))
    client = _client((_quota(), "ok"))
    client.set_capacity_gate(gate)

    with pytest.raises(RuntimeError, match="give up"):
        _invoke(client, tmp_path)

    assert len(gate.calls) == 1
