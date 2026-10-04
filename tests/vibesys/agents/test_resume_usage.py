"""Public client accounting for resumed provider threads without a baseline."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from agentshim.testing import FakeExecutor, TokenUsage, scripted_turn
from hypothesis import given
from hypothesis import strategies as st
from tests.support.run_execution import run_execution_record

from vs_agent.api import (
    AgentClient,
    AgentEvent,
    AgentEventKind,
    AgentSessionKey,
    AgentSessionState,
    AgentUsage,
    DurableSessionStore,
    SessionScope,
)
from vs_agent.api.testing import FakeDriver, fake_agentshim_driver
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _resumed_records(root: Path, *, cumulative: int, increment: int) -> list[dict]:
    (root / "OBJECTIVE.md").write_text("Reduce loop cost.\n", encoding="utf-8")
    project = Project.open(root)
    project.state.create_project("usage-regression")
    run = project.state.new_run_manifest(
        "Run 1",
        run_id="run-1",
        trusted_input_baseline="a" * 40,
        branch="vibesys/run-1",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="multi-agent", config_version=1, options={}),
    )
    project.state.create_run(run)
    slot = project.state.local_namespace("run-1", "agent").slot("sessions.json", AgentSessionState)
    store = DurableSessionStore(slot)
    key = AgentSessionKey(SessionScope.HYPOTHESIS, "H-01")
    first_driver = FakeDriver(
        turn=[AgentEvent(AgentEventKind.USAGE, usage=AgentUsage(input_tokens=100))]
    )
    with AgentClient(first_driver, provider="codex", session_store=store, log_dir=root) as client:
        _invoke(client, root, key)
    checkpoint = store.get(key)
    assert checkpoint is not None
    executor = FakeExecutor(
        [
            scripted_turn(
                "codex",
                session_id=checkpoint.session_id,
                text="done",
                usage=TokenUsage(input_tokens=count, output_tokens=count),
            )
            for count in (cumulative, cumulative + increment)
        ]
    )
    driver = fake_agentshim_driver(provider="codex", executor=executor)
    with AgentClient(driver, provider="codex", session_store=store, log_dir=root) as client:
        _invoke(client, root, key)
        _invoke(client, root, key)
    assert "resume" in executor.requests[1].argv
    return [json.loads(line) for line in (root / "usage.jsonl").read_text().splitlines()]


def _invoke(client: AgentClient, root: Path, key: AgentSessionKey) -> None:
    client.invoke_text(
        kind="implementer",
        workspace=root,
        system_prompt="test",
        user_prompt="continue",
        round_label="usage test",
        session_key=key,
    )


def test_resumed_unknown_increment_is_unknown_in_run_records(tmp_path: Path) -> None:
    records = _resumed_records(tmp_path, cumulative=200, increment=30)
    assert records[0]["input_tokens"] == 100
    assert all(records[1][field] is None for field in TOKEN_FIELDS)
    assert records[1]["total_cost_usd"] is None
    assert records[1]["duration_ms"] is not None
    assert records[2]["input_tokens"] == 30
    assert records[2]["output_tokens"] == 30


@given(cumulative=st.integers(min_value=0, max_value=1_000_000), increment=st.integers(0, 100_000))
def test_unknown_resume_never_contributes_a_measured_increment(
    cumulative: int, increment: int
) -> None:
    with TemporaryDirectory() as directory:
        records = _resumed_records(Path(directory), cumulative=cumulative, increment=increment)
    assert all(records[1][field] is None for field in TOKEN_FIELDS)
    assert sum(record["input_tokens"] or 0 for record in records) == 100 + increment
    assert records[2]["input_tokens"] == increment
