"""Current-main version-2 persisted bytes never gain fabricated v3 authority."""

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    ContractError,
    EvaluationHistoryAvailability,
    OperationRegistry,
    ProposalSubmitted,
    RunEnvelope,
    StrategyState,
    project,
    step,
    v2_to_v3_migration,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "envelope-v2-main.json"


def _load(source: str) -> RunEnvelope[StrategyState]:
    codec = OperationRegistry()
    return codec.migrate_envelope(RunEnvelope[StrategyState], source, v2_to_v3_migration(codec))


def test_current_main_persisted_state_requires_selected_migration_and_roundtrips() -> None:
    source = _FIXTURE.read_text()
    codec = OperationRegistry()
    with pytest.raises(ContractError, match="migration"):
        codec.decode_envelope(RunEnvelope[StrategyState], source)
    loaded = _load(source)
    assert loaded.schema_version == ENVELOPE_SCHEMA_VERSION
    assert (
        codec.decode_envelope(RunEnvelope[StrategyState], codec.encode_envelope(loaded)) == loaded
    )
    old = json.loads(source)
    assert loaded.core.run.model_dump(mode="json") == old["core"]["run"] | {
        "limits": old["core"]["run"]["limits"] | {"pool_capacities": []},
    }
    attempt = loaded.core.attempts.attempts[0]
    assert attempt.charges[0].charged == 1
    assert attempt.evaluation_history.availability == EvaluationHistoryAvailability.UNAVAILABLE
    assert attempt.terminal_reason is None
    assert loaded.core.sessions.run_checkpoints == ()
    continuation = loaded.core.evaluation.continuations[0]
    assert continuation.authorization_receipt is None
    assert continuation.deadline_at == old["core"]["evaluation"]["continuations"][0]["deadline_at"]
    child = loaded.core.intents.children[0]
    assert not child.watermark_history_complete
    assert len(child.observation_watermarks) == 1
    assert child.observation_watermarks[0].observation == child.observation
    result = step(loaded.core, ProposalSubmitted(decisions=(), expected_revision=loaded.revision))
    assert project(result.state).attempts[0].evaluation_history == attempt.evaluation_history
    assert result.requests == ()
    assert loaded.fence.model_dump(mode="json") == old["fence"]
    assert loaded.event_cursor.model_dump(mode="json") == old["event_cursor"]


@given(
    st.integers(min_value=0, max_value=10000),
    st.floats(min_value=0, max_value=10000, allow_nan=False),
)
def test_migration_preserves_original_source_sequences_and_deadlines(
    sequence: int, deadline: float
) -> None:
    old = json.loads(_FIXTURE.read_text())
    observation = old["core"]["intents"]["children"][0]["observation"]
    observation["sequence"] = sequence
    old["core"]["run"]["deadline_at"] = deadline
    old["core"]["evaluation"]["continuations"][0]["deadline_at"] = deadline
    loaded = _load(json.dumps(old))
    child = loaded.core.intents.children[0]
    assert child.observation_watermarks[0].observation.sequence == sequence
    assert not child.watermark_history_complete
    assert loaded.core.run.deadline_at == deadline
    assert loaded.core.evaluation.continuations[0].deadline_at == deadline
    codec = OperationRegistry()
    assert (
        codec.decode_envelope(RunEnvelope[StrategyState], codec.encode_envelope(loaded)) == loaded
    )


@pytest.mark.parametrize(
    "area", ["run", "attempts", "sessions", "evaluation", "intents", "settlement"]
)
def test_migration_rejects_unknown_keys_in_every_area(area: str) -> None:
    old = json.loads(_FIXTURE.read_text())
    old["core"][area]["fabricated_authority"] = True
    with pytest.raises(ValueError, match="fabricated_authority"):
        _load(json.dumps(old))


def test_migration_does_not_infer_other_source_watermarks_from_latest_observation() -> None:
    old = json.loads(_FIXTURE.read_text())
    child = old["core"]["intents"]["children"][0]
    child["source_requests"].append({"kind": "request", "root": "second-source"})
    loaded = _load(json.dumps(old))
    migrated = loaded.core.intents.children[0]
    assert not migrated.watermark_history_complete
    assert len(migrated.observation_watermarks) == 1
    assert len(migrated.source_requests) > len(migrated.observation_watermarks)
