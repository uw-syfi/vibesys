"""A chat turn's `propose_steer` result reaches clients as a typed `steer_proposed` event.

The tool returns a `SteerProposalReceipt` as JSON; the run server recognizes it in the chat
turn's tool result and records the proposal on the wire. Nothing here sends a steer: the event
is data for a client's card, and `command.control` is the only way a steer reaches the run.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st
from tests.server.support import ServerParts, build_server_parts

from server.events import EventType, SteerProposedData
from vibesys.api import (
    JsonResultPayload,
    SteerMode,
    SteerProposal,
    SteerProposalReceipt,
    SteerTarget,
    SteerTargetKind,
)
from vibesys.events import CoreEventType, ToolResultData
from vibesys.events import EventStatus as CoreEventStatus

_ID = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._-]{0,40}", fullmatch=True)
_TARGETS = st.one_of(
    st.just(SteerTarget()),
    st.builds(SteerTarget, kind=st.sampled_from(list(SteerTargetKind)[1:]), id=_ID),
)
_PROPOSALS = st.builds(
    SteerProposal,
    text=st.text(min_size=1).filter(lambda text: text.strip() != ""),
    target=_TARGETS,
    mode=st.sampled_from(list(SteerMode)),
)


def _receipt(proposal: SteerProposal) -> str:
    return SteerProposalReceipt(proposal=proposal, note="proposed").model_dump_json()


def _emit_tool_result(
    parts: ServerParts,
    content: str,
    *,
    agent_kind: str = "chat",
    call_id: str | None = "call-1",
    is_error: bool = False,
) -> str:
    """Emit a turn's start and one tool result, as the chat agent's event sink would."""
    execution_id = uuid.uuid4().hex
    parts.core_events.emit(
        CoreEventType.AGENT_EXECUTION_STARTED,
        status=CoreEventStatus.ACTIVE,
        agent_kind=agent_kind,
        round_label="experiment-chat",
        execution_id=execution_id,
        data=None,
    )
    parts.core_events.emit(
        CoreEventType.TOOL_RESULT,
        agent_kind=agent_kind,
        round_label="experiment-chat",
        execution_id=execution_id,
        data=ToolResultData(
            tool="mcp__vibesys-run__propose_steer",
            call_id=call_id,
            content=content,
            is_error=is_error,
            payload=JsonResultPayload(value=json.loads(content)),
        ),
    )
    return execution_id


def _proposals(parts: ServerParts) -> list[SteerProposedData]:
    return [
        event.data
        for event in parts.journal.read()
        if event.type is EventType.STEER_PROPOSED and isinstance(event.data, SteerProposedData)
    ]


@given(_PROPOSALS)
def test_a_receipt_in_a_chat_turn_becomes_one_proposal_with_the_same_fields(
    proposal: SteerProposal,
) -> None:
    with tempfile.TemporaryDirectory() as root:
        parts = build_server_parts(Path(root))
        execution_id = _emit_tool_result(parts, _receipt(proposal))

        [proposed] = _proposals(parts)

    assert proposed.proposal == proposal
    assert proposed.proposal_id == f"{execution_id}.call-1"


def test_a_proposal_follows_the_tool_result_it_came_from(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    _emit_tool_result(parts, _receipt(SteerProposal(text="keep bf16", mode=SteerMode.STANDING)))

    types = [
        event.type
        for event in parts.journal.read()
        if event.type in {EventType.TOOL_RESULT, EventType.STEER_PROPOSED}
    ]

    assert types == [EventType.TOOL_RESULT, EventType.STEER_PROPOSED]


def test_a_proposal_without_a_call_id_still_gets_a_unique_id(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    receipt = _receipt(SteerProposal(text="one"))
    _emit_tool_result(parts, receipt, call_id=None)
    _emit_tool_result(parts, receipt, call_id=None)

    ids = [proposed.proposal_id for proposed in _proposals(parts)]

    assert len(ids) == 2
    assert len(set(ids)) == 2


def test_only_a_chat_agents_receipt_proposes(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)

    _emit_tool_result(parts, _receipt(SteerProposal(text="go")), agent_kind="implementer")

    assert _proposals(parts) == []


def test_a_failed_or_foreign_tool_result_proposes_nothing(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    receipt = _receipt(SteerProposal(text="go"))

    _emit_tool_result(parts, receipt, is_error=True)
    _emit_tool_result(parts, '{"kind": "queue_status", "items": []}')
    _emit_tool_result(parts, '{"kind": "steer_proposal", "proposal": {"text": ""}, "note": ""}')

    assert _proposals(parts) == []
