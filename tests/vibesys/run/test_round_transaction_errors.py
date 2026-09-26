"""Message contracts for the named RoundTransactionError constructors."""

from __future__ import annotations

import pytest

from vibesys.run import RoundTransactionError

_CASES: list[tuple[str, tuple[object, ...], str]] = [
    ("already_completed", (3,), "Checkpoint 3 has already completed"),
    (
        "participants_disagree",
        (),
        "Checkpoint project, Git tracker, and run must agree",
    ),
    (
        "no_declared_slots",
        (),
        "Checkpoint requires at least one declared typed slot",
    ),
    ("invalid_sequence", (0,), "Checkpoint sequence must be positive, got 0"),
    (
        "unfinished_checkpoint",
        (),
        "An unfinished checkpoint already exists; recover it first",
    ),
    ("empty_writes", (), "Checkpoint writes must not be empty"),
    ("missing_head", (), "Checkpoint requires an initialized Git HEAD"),
    (
        "staged_index_changes",
        (),
        "Cannot checkpoint candidate while the Git index has staged changes",
    ),
    ("journal_sequence_mismatch", (3,), "Checkpoint journal is not for sequence 3"),
    ("undeclared_slot", ("cursor",), "Undeclared checkpoint slot 'cursor'"),
    ("invalid_journal", (ValueError("bad"),), "Invalid checkpoint journal: bad"),
    ("journal_run_mismatch", ("a",), "Checkpoint journal belongs to run 'a'"),
    ("journal_digest_mismatch", (), "Checkpoint journal digest does not match"),
    ("journal_duplicate_slot", ("cursor",), "Checkpoint journal duplicates slot 'cursor'"),
    (
        "journal_undeclared_slot",
        ("cursor",),
        "Checkpoint journal names undeclared slot 'cursor'",
    ),
    (
        "history_moved",
        ("abc123",),
        "Git history moved away from checkpoint starting commit abc123",
    ),
    (
        "committed_state_conflict",
        (),
        "Committed state differs from the checkpoint journal",
    ),
    ("snapshot_not_exact", (), "Git snapshot did not commit exact checkpoint state"),
    ("inaccessible_head", (), "Checkpoint completed without an accessible HEAD"),
]


@pytest.mark.parametrize(("factory", "args", "expected"), _CASES)
def test_round_transaction_error_factory_message(
    factory: str, args: tuple[object, ...], expected: str
) -> None:
    error = getattr(RoundTransactionError, factory)(*args)

    assert isinstance(error, RoundTransactionError)
    assert str(error) == expected
