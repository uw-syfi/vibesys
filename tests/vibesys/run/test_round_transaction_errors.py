"""Message contracts for the named RoundTransactionError constructors."""

from __future__ import annotations

import pytest

from vibesys.run import RoundTransactionError

_CASES: list[tuple[str, tuple[object, ...], str]] = [
    ("already_completed", (3,), "Round 3 transaction has already completed"),
    (
        "different_project_root",
        (),
        "Round transaction project and Git tracker must use the same project root",
    ),
    (
        "run_id_mismatch",
        ("a", "b"),
        "Round transaction run 'a' does not match Git tracker run 'b'",
    ),
    ("invalid_round_number", (0,), "Round number must be positive, got 0"),
    (
        "unfinished_transaction",
        (),
        "An unfinished round transaction already exists; recover it before starting another",
    ),
    ("already_active", (), "a completed-round transaction is already active"),
    (
        "begin_required",
        (),
        "begin_completed_round must precede project round persistence",
    ),
    ("missing_head", (), "Round transactions require an initialized Git HEAD"),
    (
        "history_moved",
        ("recover", "abc123"),
        "Cannot recover round transaction after Git history moved away "
        "from its starting commit abc123",
    ),
    ("journal_round_mismatch", (2, 3), "Journal is for round 2, not round 3"),
    (
        "agent_state_conflict",
        (),
        "Committed agent state differs from the transaction journal",
    ),
    (
        "agent_state_snapshot_not_exact",
        (),
        "Git snapshot did not commit the exact agent state",
    ),
    (
        "completed_round_snapshot_not_exact",
        (),
        "Git snapshot did not commit the exact completed-round metadata",
    ),
    ("inaccessible_head", (), "Git snapshot completed without an accessible HEAD"),
    (
        "round_payload_number_mismatch",
        (2, 3),
        "Round transaction journal payload is for round 2, not round 3",
    ),
    (
        "round_metadata_conflict",
        (),
        "Committed round metadata differs from the transaction journal",
    ),
    ("journal_missing", (), "Round transaction journal does not exist"),
    ("invalid_journal", ("bad",), "Invalid round transaction journal: bad"),
    (
        "journal_run_mismatch",
        ("a", "b"),
        "Round transaction journal belongs to run 'a', not 'b'",
    ),
    (
        "payload_digest_mismatch",
        ("payload",),
        "Round transaction journal payload digest does not match",
    ),
    (
        "invalid_active_transition",
        ("bad",),
        "Invalid active-state transition in round transaction journal: bad",
    ),
    (
        "invalid_agent_state_transition",
        ("bad",),
        "Invalid agent-state transition in round transaction journal: bad",
    ),
    (
        "staged_index_changes",
        (),
        "Cannot begin round transaction while the Git index contains staged changes",
    ),
    (
        "invalid_transition",
        ("bad",),
        "Invalid round transaction agent-state transition: bad",
    ),
    (
        "invalid_round_payload",
        ("src", "bad"),
        "Invalid completed-round payload in transaction journal src: bad",
    ),
    (
        "round_payload_not_object",
        ("src",),
        "Invalid completed-round payload in transaction journal src: payload must be a JSON object",
    ),
]


@pytest.mark.parametrize(("factory", "args", "expected"), _CASES)
def test_round_transaction_error_factory_message(
    factory: str, args: tuple[object, ...], expected: str
) -> None:
    error = getattr(RoundTransactionError, factory)(*args)

    assert isinstance(error, RoundTransactionError)
    assert str(error) == expected
