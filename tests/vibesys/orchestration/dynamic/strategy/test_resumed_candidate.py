"""A resumed implementer settles only after its own checkpoint outcome is known."""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from tests.vibesys.orchestration.dynamic.strategy._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.orchestration.dynamic.strategy._run import run_shell
from tests.vibesys.orchestration.dynamic.strategy._waiting_turns import (
    WaitingTurns,
    attach_for,
    new_bridge,
    translator_for,
)

from vs_core.api import RunStatus, Settle, Withdraw
from vs_runtime.api.testing import HostOptions, Run

if TYPE_CHECKING:
    from pathlib import Path

    from vs_core.api import AttemptCheckpoint


def _run_resumed(root: Path, *, unchanged: bool) -> Run:
    bridge = new_bridge(root)
    script = WaitingTurns(
        planner=deque(
            [plan_reply(implement("h0")), *(plan_reply(implement(f"h{n}")) for n in range(1, 7))]
        ),
        implementer=deque(implemented("supported") for _ in range(60)),
        judge=deque(reviewed(passed=True) for _ in range(60)),
        decide=lambda _ordinal: True,
        delay=lambda _ordinal: 0,
        polls=[2],
        roles=("implementer",),
        retain_per_turn=True,
        unchanged_after_first_retain=unchanged,
        benchmark=lambda _commit: 100.0,
    )
    host = HostOptions(
        max_concurrent=3,
        translator=translator_for(script, bridge),
        attach=attach_for(script, bridge),
        controlled=True,
        lifecycle=frozenset({"suspend"}),
    )
    return run_shell(script, host=host, max_rounds=1, max_in_flight=1)


def _settled(finished: Run) -> tuple[Settle, tuple[AttemptCheckpoint, ...]]:
    assert finished.core.run.status == RunStatus.TERMINAL
    (settle,) = [
        item.disposition
        for item in finished.decisions
        if isinstance(item, Withdraw) and isinstance(item.disposition, Settle)
    ]
    (attempt,) = finished.core.attempts.attempts
    turn_checkpoints = tuple(item for item in attempt.checkpoints if item.invocation is not None)
    return settle, turn_checkpoints


def test_an_unchanged_resume_records_and_uses_its_current_revision(
    tmp_path: Path,
) -> None:
    settle, checkpoints = _settled(_run_resumed(tmp_path, unchanged=True))

    assert len(checkpoints) == 2
    assert checkpoints[0].revision == checkpoints[1].revision
    assert settle.candidate == checkpoints[1].revision


def test_a_changed_resume_uses_its_own_newer_checkpoint(tmp_path: Path) -> None:
    settle, checkpoints = _settled(_run_resumed(tmp_path, unchanged=False))

    assert len(checkpoints) == 2
    assert checkpoints[0].revision != checkpoints[1].revision
    assert settle.candidate == checkpoints[1].revision
