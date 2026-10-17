"""The agent invocation ledger decodes strictly, and its shape is pinned to its version.

The ledger (``AgentInvocationState``) stores each completed turn's ``AgentTurnResult``, and a
restarted host settles a finished turn from it, refusals of mid-turn steers included. A stored
result with a key this version does not know, or without a key it writes, is refused rather
than decoded with the refusals silently lost; a change of the stored shape needs a new
``schema_version``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import ValidationError

from vs_agent.api import (
    AgentInvocationRecord,
    AgentInvocationState,
    AgentSessionCheckpoint,
    AgentSessionKey,
    AgentTurnResult,
    Completed,
    SessionScope,
)
from vs_core.api.testing import assert_schema_pinned

_VERSIONS = Path(__file__).parent / "invocation_ledger_schema_versions.txt"
_KEY = str(AgentSessionKey(SessionScope.HYPOTHESIS, "H-01"))


def _ledger(refused: tuple[str, ...]) -> AgentInvocationState:
    completed = Completed(
        session_key=_KEY,
        invocation_id="turn-1",
        result=AgentTurnResult(text="done", provider_session_id="p", refused_steers=refused),
        checkpoint=AgentSessionCheckpoint(session_key=_KEY, provider_session_id="p"),
    )
    record = AgentInvocationRecord(payload_digest="digest", outcome=completed)
    return AgentInvocationState(invocations={"turn-1": record})


@given(st.lists(st.text(min_size=1, max_size=8), max_size=3, unique=True).map(tuple))
@example(("steer-op-1",))
def test_the_refusals_of_a_stored_turn_survive_a_reload(refused: tuple[str, ...]) -> None:
    ledger = _ledger(refused)
    loaded = AgentInvocationState.model_validate_json(ledger.model_dump_json())
    outcome = loaded.invocations["turn-1"].outcome
    assert isinstance(outcome, Completed)
    assert outcome.result.refused_steers == refused


@pytest.mark.parametrize("change", ["unknown-key", "missing-refusals"])
def test_a_stored_result_this_version_did_not_write_is_refused(change: str) -> None:
    stored = json.loads(_ledger(("steer-op-1",)).model_dump_json())
    result = stored["invocations"]["turn-1"]["outcome"]["result"]
    if change == "unknown-key":
        result["refused_steer_ids"] = result.pop("refused_steers")
    else:
        del result["refused_steers"]
    with pytest.raises(ValidationError, match="refused_steer"):
        AgentInvocationState.model_validate(stored)


def test_the_ledger_shape_is_the_one_pinned_for_its_version() -> None:
    version = AgentInvocationState.model_fields["schema_version"].default
    assert_schema_pinned(
        _VERSIONS.read_text(encoding="utf-8"),
        (version,),
        AgentInvocationState.model_json_schema(),
    )
