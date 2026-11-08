"""Draft plan, pre-plan microbench phase, revised plan (MICROBENCH_PHASE_ENV)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from vibesys.orchestration.hypothesis import OrchestratorPlan
from vibesys.orchestration.multi.turns import MICROBENCH_PHASE_ENV, MicrobenchPhaseResponse

from .test_resilience import (
    DESIGNER,
    IMPLEMENTER,
    JUDGE,
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


def _revised() -> OrchestratorPlan:
    plan = _plan()
    plan.title = "Batch prefill on the measured design"
    plan.task = "Build the measured best design, then batch prefill."
    return plan


def _plan_file(path: Path) -> dict:
    found = sorted(path.rglob("plans/round-0001.json"))
    assert found, "no plan file"
    return json.loads(found[-1].read_text())


def test_draft_is_measured_and_revised_before_the_implementer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MICROBENCH_PHASE_ENV, "1")
    script = _Script(
        _pre_round(),
        _plan(),
        MicrobenchPhaseResponse(summary="Rows abc1234567 contradict the draft's design."),
        _revised(),
        _implementation(),
        _judge(),
    )
    _run(tmp_path, script)
    roles = [role for role, _history, _message in script.calls]
    assert roles == [DESIGNER.id, DESIGNER.id, IMPLEMENTER.id, DESIGNER.id,
                     IMPLEMENTER.id, JUDGE.id]
    measure, revise, implement = script.calls[2], script.calls[3], script.calls[4]
    assert "round-0001" in measure[2] and "microbench" in measure[2]
    # The revision continues the draft's conversation and carries the summary.
    assert revise[1] and "abc1234567" in revise[2]
    assert "no fallback" in revise[2]
    assert "inside the kernel" in revise[2] and "ledger" in revise[2]
    # The microbench phase and the implementer are separate conversations.
    assert implement[1] == ()
    assert "Batch prefill on the measured design" in json.dumps(_plan_file(tmp_path))


def test_without_the_variable_the_plan_goes_straight_to_the_implementer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(MICROBENCH_PHASE_ENV, raising=False)
    script = _Script(_pre_round(), _plan(), _implementation(), _judge())
    _run(tmp_path, script)
    assert [role for role, _h, _m in script.calls] == [DESIGNER.id, DESIGNER.id,
                                                      IMPLEMENTER.id, JUDGE.id]


def test_the_phase_gets_a_deadline_only_when_a_limit_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vibesys.orchestration.multi.turns import MICROBENCH_MINUTES_ENV

    monkeypatch.setenv(MICROBENCH_PHASE_ENV, "1")
    for limit, expect in (("30", True), ("", False)):
        monkeypatch.setenv(MICROBENCH_MINUTES_ENV, limit)
        script = _Script(_pre_round(), _plan(), MicrobenchPhaseResponse(summary="s"),
                         _revised(), _implementation(), _judge())
        _run(tmp_path / (limit or "none"), script)
        assert ("deadline: " in script.calls[2][2]) is expect


def test_past_the_full_rounds_no_open_question_skips_the_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vibesys.orchestration.multi.turns import (
        MICROBENCH_FULL_ROUNDS_ENV,
        OpenQuestionsResponse,
    )

    monkeypatch.setenv(MICROBENCH_PHASE_ENV, "1")
    monkeypatch.setenv(MICROBENCH_FULL_ROUNDS_ENV, "0")
    script = _Script(_pre_round(), _plan(), OpenQuestionsResponse(questions=[], reason="r"),
                     _implementation(), _judge())
    _run(tmp_path, script)
    roles = [role for role, _h, _m in script.calls]
    assert roles == [DESIGNER.id, DESIGNER.id, DESIGNER.id, IMPLEMENTER.id, JUDGE.id]
    assert "timeline" in script.calls[2][2]
    assert sorted(tmp_path.rglob("microbench/skipped/round-0001.md"))


def test_past_the_full_rounds_only_the_open_questions_are_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vibesys.orchestration.multi.turns import (
        MICROBENCH_FULL_ROUNDS_ENV,
        OpenQuestionsResponse,
    )

    monkeypatch.setenv(MICROBENCH_PHASE_ENV, "1")
    monkeypatch.setenv(MICROBENCH_FULL_ROUNDS_ENV, "0")
    script = _Script(
        _pre_round(), _plan(),
        OpenQuestionsResponse(questions=["price the unbuilt design Q7"], reason="r"),
        MicrobenchPhaseResponse(summary="s"), _revised(), _implementation(), _judge(),
    )
    _run(tmp_path, script)
    roles = [role for role, _h, _m in script.calls]
    assert roles == [DESIGNER.id, DESIGNER.id, DESIGNER.id, IMPLEMENTER.id, DESIGNER.id,
                     IMPLEMENTER.id, JUDGE.id]
    assert "only the open questions" in script.calls[3][2]
    assert "unbuilt design Q7" in script.calls[3][2]


def test_within_the_full_rounds_the_whole_phase_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vibesys.orchestration.multi.turns import MICROBENCH_FULL_ROUNDS_ENV

    monkeypatch.setenv(MICROBENCH_PHASE_ENV, "1")
    monkeypatch.setenv(MICROBENCH_FULL_ROUNDS_ENV, "1")
    script = _Script(_pre_round(), _plan(), MicrobenchPhaseResponse(summary="s"),
                     _revised(), _implementation(), _judge())
    _run(tmp_path, script)
    assert "only the open questions" not in script.calls[2][2]
    assert [r for r, _h, _m in script.calls].count(IMPLEMENTER.id) == 2
