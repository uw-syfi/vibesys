"""An implementer turn that returns continue without a new measurement is resumed once."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration.multi.turns import MEASUREMENT_DIR_ENV

from .test_resilience import (
    IMPLEMENTER,
    _implementation,
    _judge,
    _plan,
    _pre_round,
    _run,
    _Script,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _unfinished():
    return _implementation(hypothesis_outcome="continue", next_step="Read the check log.")


def test_continue_without_measurement_is_resumed_in_the_same_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MEASUREMENT_DIR_ENV, "reports")
    script = _Script(_pre_round(), _plan(), _unfinished(), _implementation(), _judge())
    _run(tmp_path, script)
    implementer = [c for c in script.calls if c[0] == IMPLEMENTER.id]
    assert len(implementer) == 2
    assert "no new measurement" in implementer[1][2]
    assert "foreground" in implementer[1][2]


def test_continue_with_a_new_measurement_is_not_resumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MEASUREMENT_DIR_ENV, "reports")
    script = _Script(_pre_round(), _plan(), _unfinished(), _judge())
    respond = script.respond

    def measuring(role, history, message, response):
        if role.id == IMPLEMENTER.id:
            for workspace in [p for p in tmp_path.rglob("*") if p.is_dir()] + [tmp_path]:
                (workspace / "reports").mkdir(exist_ok=True)
                (workspace / "reports" / f"check-{len(script.calls)}.jsonl").write_text("{}")
        return respond(role, history, message, response)

    script.respond = measuring
    _run(tmp_path, script)
    implementer = [c for c in script.calls if c[0] == IMPLEMENTER.id]
    assert all("no new measurement" not in c[2] for c in implementer)


def test_without_the_setting_nothing_is_resumed(tmp_path: Path) -> None:
    script = _Script(_pre_round(), _plan(), _unfinished(), _judge())
    _run(tmp_path, script)
    implementer = [c for c in script.calls if c[0] == IMPLEMENTER.id]
    assert all("no new measurement" not in c[2] for c in implementer)
