"""A lost suspension envelope fences replay after the WIP has been retained."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.dynamic.test_plugin_suspension import _open

from vibesys.orchestration.dynamic.lifecycle import IntentKind, IntentStage
from vibesys.orchestration.dynamic.models import DurableStateCommitError, DynamicState
from vs_runtime.api import RuntimeContractError

if TYPE_CHECKING:
    from pathlib import Path


def test_retained_wip_with_uncommitted_suspension_blocks_initial_turn_replay(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        opened = await _open(tmp_path)
        opened.run.state.script_commit_at(
            "dynamic: evaluation suspension WorkerAwaitingEvaluation",
            RuntimeError("simulated crash after retain before envelope acknowledgement"),
        )
        task = opened.start()
        with pytest.raises(DurableStateCommitError):
            await opened.waiting(task)
        durable = await opened.run.state.load(DynamicState)
        assert durable is not None
        assert durable.lifecycle.continuations == {}
        assert durable.workstreams[0].budget.spent == 1
        assert len(opened.calls) == 1
        candidate = opened.run.workspaces.candidates[-1]
        retained = candidate.retained["dynamic-held-suspended"]
        assert opened.run.workspaces.root.knows_revision(retained)
        with pytest.raises(RuntimeContractError, match="requires reconciliation"):
            await opened.start()
        recovered = await opened.run.state.load(DynamicState)
        assert recovered is not None
        assert recovered.workstreams[0].budget == durable.workstreams[0].budget
        assert recovered.search.rounds == []
        assert len(opened.calls) == 1
        assert any(
            intent.kind is IntentKind.TURN and intent.stage is IntentStage.BLOCKED
            for intent in recovered.lifecycle.intents.values()
        )
        assert opened.run.workspaces.root.knows_revision(retained)
        opened.client.close()

    asyncio.run(scenario())
