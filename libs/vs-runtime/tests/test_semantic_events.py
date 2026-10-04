"""Blocked-intent diagnostics: one row per request identity, command-only acknowledgement."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.observation_contract import assert_core_accepts
from tests.support.runtime_operations import SCOPE

from vs_core.api import BlockIntent, HostFence, HostId, ObservationStatus, RequestId
from vs_project.api import Project
from vs_runtime.api.core import ExecutionContext, JournalSemanticEvents, RequestExecutors

pytestmark = pytest.mark.asyncio

CONTEXT = ExecutionContext(
    fence=HostFence(host_id=HostId(root="h"), epoch=1), now_at=3.0, payload_digest="d"
)


def block(request: str, target: str, text: str) -> BlockIntent:
    return BlockIntent(
        request_id=RequestId(root=request),
        scope=SCOPE,
        deadline_at=100.0,
        target=RequestId(root=target),
        diagnostic=text,
    )


def journal(root: Path) -> JournalSemanticEvents:
    (root / "project").mkdir(exist_ok=True)
    return JournalSemanticEvents(Project.open(root / "project").state.state_store_namespace("run"))


async def test_acknowledgement_covers_the_command_only_and_claims_no_release() -> None:
    with tempfile.TemporaryDirectory() as raw:
        events = journal(Path(raw))
        result = await events.execute(block("b1", "t1", "stuck"), CONTEXT)
        observation = result.observation
        assert result.observation.target is None
        assert observation.observation.status is ObservationStatus.SUCCEEDED
        assert observation.observation.accepted
        assert not observation.observation.released
        assert not observation.observation.children_complete
        assert [row.diagnostic for row in events.read()] == ["stuck"]


@given(
    deliveries=st.lists(
        st.tuples(st.sampled_from(["a", "b", "c"]), st.sampled_from(["x", "y"])), max_size=12
    )
)
async def test_redelivery_never_adds_a_row_and_conflicts_are_rejected(
    deliveries: list[tuple[str, str]],
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        events = journal(Path(raw))
        first: dict[str, str] = {}
        for request, text in deliveries:
            attempt = block(request, f"target-{request}", text)
            if request in first and first[request] != text:
                rejected = await events.execute(attempt, CONTEXT)
                assert rejected.observation.observation.status is ObservationStatus.REJECTED
                continue
            await events.execute(attempt, CONTEXT)
            first.setdefault(request, text)
        rows = events.read()
        assert [row.request_id for row in rows] == list(first)
        assert [row.sequence for row in rows] == list(range(1, len(rows) + 1))
        assert [row.diagnostic for row in rows] == list(first.values())


async def test_a_new_host_sees_the_journal_a_crashed_host_wrote() -> None:
    with tempfile.TemporaryDirectory() as raw:
        await journal(Path(raw)).execute(block("b1", "t1", "stuck"), CONTEXT)
        restarted = journal(Path(raw))
        again = await restarted.execute(block("b1", "t1", "stuck"), CONTEXT)
        assert len(restarted.read()) == 1
        assert again.observation.observation.status is ObservationStatus.SUCCEEDED


async def test_request_executors_route_block_intent_to_the_semantic_role() -> None:
    with tempfile.TemporaryDirectory() as raw:
        events = journal(Path(raw))
        executors = RequestExecutors(semantic_events=events)
        request = block("b1", "t1", "stuck")
        assert executors.refusal(request) is None
        await executors.dispatch(request, CONTEXT)
        assert len(events.read()) == 1


async def test_core_accepts_a_rejected_conflict_after_the_published_acknowledgement() -> None:
    with tempfile.TemporaryDirectory() as raw:
        events = journal(Path(raw))
        published = await events.execute(block("r1", "t1", "waiting"), CONTEXT)
        conflict = await events.execute(block("r1", "t1", "another reason"), CONTEXT)
        replayed = await events.execute(block("r1", "t1", "another reason"), CONTEXT)
        assert conflict.observation.observation.status is ObservationStatus.REJECTED
        assert_core_accepts([published, conflict, replayed])
