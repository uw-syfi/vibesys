from datetime import UTC, datetime
from pathlib import Path

from vibesys.repository import generate_experiment_name, repository_name_from_experiment


def test_generate_experiment_name_uses_input_and_timestamp():
    assert (
        generate_experiment_name(
            Path("examples/data-structures/Queue MPSC"),
            now=datetime(2026, 7, 20, 12, 34, 56, tzinfo=UTC),
        )
        == "queue-mpsc-20260720-123456"
    )


def test_repository_name_sanitizes_an_explicit_experiment_name():
    assert repository_name_from_experiment("My queue trial #4") == "my-queue-trial-4"
