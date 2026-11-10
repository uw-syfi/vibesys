"""A run seeded from earlier runs numbers its rounds after theirs."""

from vibesys.orchestration.multi import orchestration


def test_rounds_before_reads_the_seeded_count(monkeypatch):
    monkeypatch.setenv(orchestration.ROUNDS_BEFORE_ENV, "3")
    assert orchestration.rounds_before() == 3


def test_rounds_before_is_zero_for_a_fresh_or_bad_value(monkeypatch):
    monkeypatch.delenv(orchestration.ROUNDS_BEFORE_ENV, raising=False)
    assert orchestration.rounds_before() == 0
    monkeypatch.setenv(orchestration.ROUNDS_BEFORE_ENV, "x")
    assert orchestration.rounds_before() == 0
