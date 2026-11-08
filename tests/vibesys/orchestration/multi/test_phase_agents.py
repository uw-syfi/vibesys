"""Phase rounds (PHASE_AGENTS_ENV): the coordinator paragraph and the plan sentence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration.multi.turns import (
    MICROBENCH_FULL_ROUNDS_ENV,
    PHASE_AGENTS_ENV,
    _phase_round,
)

from .test_resilience import DESIGNER, IMPLEMENTER, _implementation, _judge, _plan, _pre_round, _run, _Script

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _calls(tmp_path: Path) -> tuple[str, str]:
    script = _Script(_pre_round(), _plan(), _implementation(), _judge())
    _run(tmp_path, script)
    plan = next((m for r, _h, m in script.calls if r == DESIGNER.id and "phase round" in m), "")
    implement = next(m for r, _h, m in script.calls if r == IMPLEMENTER.id)
    return plan, implement


def test_a_phase_round_names_the_coordinator_duties(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PHASE_AGENTS_ENV, "1")
    monkeypatch.setenv(MICROBENCH_FULL_ROUNDS_ENV, "0")
    plan, implement = _calls(tmp_path)
    assert "Phase table and phase rounds" in implement
    assert "`phase` subagent" in implement
    assert "name the target phases" in plan


def test_a_blueprint_round_has_no_phase_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PHASE_AGENTS_ENV, "1")
    monkeypatch.setenv(MICROBENCH_FULL_ROUNDS_ENV, "1")
    plan, implement = _calls(tmp_path)
    assert "phase round" not in implement and not plan


def test_without_the_variable_there_is_no_phase_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(PHASE_AGENTS_ENV, raising=False)
    monkeypatch.setenv(MICROBENCH_FULL_ROUNDS_ENV, "0")
    plan, implement = _calls(tmp_path)
    assert "phase round" not in implement and not plan


def test_an_unset_round_count_means_two(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PHASE_AGENTS_ENV, "1")
    monkeypatch.delenv(MICROBENCH_FULL_ROUNDS_ENV, raising=False)
    assert [_phase_round(n) for n in (1, 2, 3)] == [False, False, True]
