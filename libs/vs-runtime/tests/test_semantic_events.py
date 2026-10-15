"""Blocked-record diagnostics: one row per request identity, command-only acknowledgement."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import BlockRequest, FailureReason, ObservationStatus, RequestId
from vs_project.api import Project
from vs_runtime.api.core import JournalSemanticEvents, ReceiptStore, RequestExecutors
from vs_runtime.api.executor_contexts import context_for
from vs_runtime.api.observation_contracts import assert_core_accepts
from vs_runtime.api.operation_fixtures import SCOPE
from vs_sim.api import OsThreads

pytestmark = pytest.mark.asyncio


def block(request: str, target: str, reason: FailureReason) -> BlockRequest:
    return BlockRequest(
        request_id=RequestId(root=request),
        scope=SCOPE,
        target=RequestId(root=target),
        reason=reason,
    )


def journal(root: Path) -> JournalSemanticEvents:
    (root / "project").mkdir(exist_ok=True)
    namespace = Project.open(root / "project").state.state_store_namespace("run")
    return JournalSemanticEvents(ReceiptStore(namespace, threads=OsThreads()), namespace)


async def test_acknowledgement_covers_the_command_only_and_claims_no_release() -> None:
    with tempfile.TemporaryDirectory() as raw:
        events = journal(Path(raw))
        result = await events.execute(
            block("b1", "t1", FailureReason.LOST),
            context_for(block("b1", "t1", FailureReason.LOST)),
        )
        observation = result.observation
        assert result.observation.target is None
        assert observation.observation.status is ObservationStatus.SUCCEEDED
        assert observation.observation.accepted
        assert not observation.observation.released
        assert not observation.observation.children_complete
        assert [row.reason for row in events.read()] == [FailureReason.LOST]


@given(
    deliveries=st.lists(
        st.tuples(
            st.sampled_from(["a", "b", "c"]),
            st.sampled_from([FailureReason.LOST, FailureReason.REFUSED]),
        ),
        max_size=12,
    )
)
async def test_redelivery_never_adds_a_row_and_conflicts_are_rejected(
    deliveries: list[tuple[str, FailureReason]],
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        events = journal(Path(raw))
        first: dict[str, FailureReason] = {}
        for request, text in deliveries:
            attempt = block(request, f"target-{request}", text)
            if request in first and first[request] != text:
                rejected = await events.execute(attempt, context_for(attempt))
                assert rejected.observation.observation.status is ObservationStatus.REJECTED
                continue
            await events.execute(attempt, context_for(attempt))
            first.setdefault(request, text)
        rows = events.read()
        assert [row.request_id for row in rows] == list(first)
        assert [row.sequence for row in rows] == list(range(1, len(rows) + 1))
        assert [row.reason for row in rows] == list(first.values())


async def test_a_new_host_sees_the_journal_a_crashed_host_wrote() -> None:
    with tempfile.TemporaryDirectory() as raw:
        await journal(Path(raw)).execute(
            block("b1", "t1", FailureReason.LOST),
            context_for(block("b1", "t1", FailureReason.LOST)),
        )
        restarted = journal(Path(raw))
        again = await restarted.execute(
            block("b1", "t1", FailureReason.LOST),
            context_for(block("b1", "t1", FailureReason.LOST)),
        )
        assert len(restarted.read()) == 1
        assert again.observation.observation.status is ObservationStatus.SUCCEEDED


async def test_request_executors_route_block_record_to_the_semantic_role() -> None:
    with tempfile.TemporaryDirectory() as raw:
        events = journal(Path(raw))
        executors = RequestExecutors(semantic_events=events)
        request = block("b1", "t1", FailureReason.LOST)
        assert executors.refusal(request) is None
        await executors.dispatch(request, context_for(request))
        assert len(events.read()) == 1


async def test_core_accepts_a_rejected_conflict_after_the_published_acknowledgement() -> None:
    with tempfile.TemporaryDirectory() as raw:
        events = journal(Path(raw))
        published = await events.execute(
            block("r1", "t1", FailureReason.TIMED_OUT),
            context_for(block("r1", "t1", FailureReason.TIMED_OUT)),
        )
        conflict = await events.execute(
            block("r1", "t1", FailureReason.OWNER_GONE),
            context_for(block("r1", "t1", FailureReason.OWNER_GONE)),
        )
        replayed = await events.execute(
            block("r1", "t1", FailureReason.OWNER_GONE),
            context_for(block("r1", "t1", FailureReason.OWNER_GONE)),
        )
        assert conflict.observation.observation.status is ObservationStatus.REJECTED
        assert_core_accepts([published, conflict, replayed])
