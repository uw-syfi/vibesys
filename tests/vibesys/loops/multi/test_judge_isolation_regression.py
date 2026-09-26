"""Regression test for a live-run crash: a ReadOnly judge's stray write
inside a declared-memory path used to survive isolation revert and then
fail the post-restore unauthorized check.

A live Haiku run of the profile-guided-multi-agent preset (log at
``/tmp/vs-liverun-logs/pg-multi.log``) crashed with::

    vibesys.runtime.RoleIsolationError: Cannot isolate judge: workspace is
    still modified after restore: progress-artifacts/evidence/round-0001-
    attempt-02-judge.json

Round 1's log shows the judge running *twice*: attempt 1's judge PASSed
cleanly (no stray writes; ``vibesys.loops.multi.session.MultiSession.review``
only calls ``_validate_local`` -- the framework recipe gate -- *after* a
PASS verdict), then that framework validation recipe failed (an
unavailable ``file`` command), which is what triggered the retry to
attempt 2, not a judge FAIL. Attempt 2's judge, mid-turn (at the 110s mark
inside its own turn, long after that turn's pre-turn snapshot), issued its
own ``Write`` tool call to
``progress-artifacts/evidence/round-0001-attempt-02-judge.json``, mirroring
the implementer's own evidence-artifact naming convention. This is
confirmably *not* a framework write landing inside the turn's snapshot
window: ``orchestration/artifacts.py`` has no ``*-judge.json`` writer at
all (only ``*-implementer.json``/``*-implementer.started.json`` under
``evidence/`` and ``round-*-attempt-*.json`` under ``validation/``), and
the one framework write ``review()`` does make before the judge's turn
(``_write_implementer_artifact``, producing
``round-0001-attempt-02-implementer.json``) happens *before*
``ctx.agents.turn`` takes its pre-turn snapshot, so it is already part of
the committed baseline the judge's turn started from -- the log's own
``ls -la progress-artifacts/evidence/`` (run by the judge itself, 18s into
its turn) already shows that file on disk before the judge does anything.
So this was entirely the judge's own unauthorized action, not a framework
artifact race. ``MULTI_JUDGE`` is ``ReadOnly()`` with an empty
``access.allow``, so this stray write should have been reverted like any
other unauthorized edit.

This test reproduces the same attempt-2-judge shape through a scripted
:class:`FakeAgentClient` (judge FAILs attempt 1, then stray-writes the
evidence file and PASSes attempt 2 -- a judge-verdict retry rather than the
live run's framework-validation retry, but the same crash condition: a
``ReadOnly`` role writing into a declared-memory path during its turn) and
drives ``ProfileGuidedMultiAgentOrchestrator`` end to end via
``run_orchestration`` against fakes only (``FakeAgentClient``,
``FakeComputeBackend`` through ``run_agent_loop``'s default
``backend_factory``, and the real (non-scripted) gate executor is never
reached since ``max_rounds=1`` uses the stub backend that short-circuits
framework gates, matching ``tests/vibesys/golden/test_profile_multi_golden.py``).
No live agent CLI ever runs.

Before the fix (``workspaces.restore`` unconditionally preserving declared-
memory paths during a ``ReadOnly`` role's isolation revert), this test
raises :class:`~vibesys.runtime.RoleIsolationError` -- confirmed by running
it against the pre-fix ``agents.py``/``workspaces.py`` via a throwaway
``git stash`` of just those two files. After the fix (the isolation revert
passing ``preserve_memory=False``), the stray write is fully reverted and
the round completes normally.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch  # test-isolation: attribution scripted below

import pytest
from tests.vibesys.loops._support import run_agent_loop

from vibesys.evaluators.input_manifest import ProfileGuidedInput
from vibesys.evaluators.metrics import MetricSpace
from vibesys.loops.agent_options import AgentOrchestrationOptions, descriptor_from_options
from vibesys.loops.multi.orchestration import ProfileGuidedMultiAgentOrchestrator
from vibesys.roles.common import Verdict
from vibesys.roles.implementer import ImplementerResponse
from vibesys.roles.judge import JudgeResponse
from vibesys.roles.pre_round import PreRoundDecision
from vibesys.search.hypothesis import OrchestratorPlan
from vs_agent.api.testing import FakeAgentClient

if TYPE_CHECKING:
    from pathlib import Path

    from vs_agent.api.testing import FakeInvocation

_ORCHESTRATION_ID = "profile-guided-multi-agent"

#: Same path the live run's judge stray-wrote (round 1, attempt 2).
_EVIDENCE_RELATIVE_PATH = "progress-artifacts/evidence/round-0001-attempt-02-judge.json"


def _options() -> AgentOrchestrationOptions:
    return AgentOrchestrationOptions.model_validate(
        {
            "interface": "inprocess",
            "max_rounds": 1,
            "max_retries_per_round": 2,
            "judge_every": 1,
            "official_eval_every": 1,
            "memory_layout": "files",
            "metric_space": MetricSpace(),
            "profile_guided": ProfileGuidedInput(command=("profile",)),
        }
    )


def _decision() -> PreRoundDecision:
    return PreRoundDecision(
        need_profile=False, profile_focus="", reasoning="scripted: skip profiling"
    )


def _plan() -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id="r1-h1-enqueue-binary-insertion",
        hypothesis="the enqueue path incurs most of the measured cost",
        task="replace the enqueue insertion algorithm",
        pass_criteria="correctness gates pass; enqueue cost drops",  # noqa: S106  # LW-040133 [S106]; the argument is a fixture literal, not a credential.
        reasoning="scripted: reproduce the live-run judge isolation crash",
    )


def _implementer(summary: str) -> ImplementerResponse:
    return ImplementerResponse(
        summary=summary,
        expected_behavior="lower enqueue cost",
        evidence="ran the local checks",
    )


def _judge_fail() -> JudgeResponse:
    return JudgeResponse(
        analysis="attempt 1: framework validation recipe was not portable",
        feedback="fix the validation recipe",
        verdict=Verdict.FAIL,
    )


def _judge_stray_write_then_pass(invocation: FakeInvocation) -> JudgeResponse:
    """Attempt 2's judge: mirror the live run's own rogue evidence write.

    A real judge turn never has ``Writes`` access, so this stray write must
    be reverted by ``ctx.agents.turn``'s ``ReadOnly`` isolation, not by
    anything in this test.
    """
    evidence_path = invocation.workspace / _EVIDENCE_RELATIVE_PATH
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text('{"verdict": "pass"}\n', encoding="utf-8")
    return JudgeResponse(
        analysis="attempt 2: validation recipe repaired, implementation correct",
        feedback="",
        verdict=Verdict.PASS,
    )


# test-isolation: run_attribution has no injectable seam yet; the test scripts it out
def _no_attribution() -> AsyncMock:
    """Replace the per-round component-attribution shell-out with an empty
    result, the same seam ``tests/vibesys/golden/test_profile_multi_golden.py``
    uses: keeps the real ``run_attribution`` call site in
    ``vibesys.loops.multi.session`` but removes the dependency on a real
    profiler command running in the sandbox.
    """
    # test-isolation: run_attribution has no injectable seam yet; the test scripts it out
    return AsyncMock(return_value=())


def test_judge_stray_write_in_declared_memory_path_is_reverted(tmp_path: Path) -> None:
    """The judge's stray evidence write is fully reverted; the round
    completes instead of crashing with ``RoleIsolationError``.
    """
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue("orchestrator", _decision(), _plan())
    runner.enqueue(
        "implementer",
        _implementer("attempt 1: initial binary-search insertion"),
        _implementer("attempt 2: repaired validation recipe"),
    )
    runner.enqueue("judge", _judge_fail(), _judge_stray_write_then_pass)

    descriptor = descriptor_from_options(_options(), orchestration_id=_ORCHESTRATION_ID)
    # test-isolation: run_attribution has no injectable seam yet; the test scripts it out
    with patch("vibesys.loops.multi.session.run_attribution", new=_no_attribution()):
        run = run_agent_loop(
            tmp_path,
            runner,
            ProfileGuidedMultiAgentOrchestrator,
            descriptor,
            exp_name="judge-isolation-regression",
        )

    assert run.result is True
    assert len(runner.calls_for("judge")) == 2
    # The stray write never survives as a "preserved" declared-memory
    # leftover: it is reverted like any other unauthorized ReadOnly edit.
    assert not (run.project_dir / _EVIDENCE_RELATIVE_PATH).exists()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
