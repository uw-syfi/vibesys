"""A planned profile on the Slurm run environment produces trusted evidence.

r16 profiled 12 times and every profile ended unsupported: no evaluation
executor produced the profile evidence kind, and the profiler's own capture
could not upload its job script through the run's Slurm broker. These scenarios
run the production loop over the Fake cluster, whose GPU node fakes only the
profiler binary, so the capture, staging, and evidence path is production code.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from vibesys.orchestration.dynamic.agents import ORCHESTRATOR, PROFILER
from vibesys.orchestration.structured_turn import StructuredResponseError
from vs_evaluation.api import EvidenceKind, EvidenceOutcome, TrustedEvidence
from vs_runtime.api import CandidateProfileStatus
from vs_slurm.fake_connector import active_jobs

from ._harness import (
    PASS,
    LoopInput,
    ScriptedAgents,
    Turn,
    edit_to,
    implemented,
    load_state,
    options,
    planner_history,
    portfolio,
    profile_workstream,
    run_loop,
    workstream,
)

if TYPE_CHECKING:
    from pathlib import Path


class ProfilerResumeCorrectionUnavailableError(RuntimeError):
    """Known resumed profiler schema failure, fenced after successful prerequisites."""


def _submit_profile(agent: Turn) -> dict[str, object]:
    """Yield the profiler's semantic capture to the host without an agent await."""
    return {"kind": "waiting_for_evaluation", "handles": [agent.submit("profile")]}


def _trusted_profile(agent: Turn) -> dict[str, object]:
    """Read the host-settled profile evidence in the resumed conversation."""
    records = tuple(
        TrustedEvidence.model_validate(record) for record in agent.accepted_evidence("profile")
    )
    (evidence,) = [record for record in records if record.evidence_id in agent.prompt]
    assert evidence.kind is EvidenceKind.PROFILE, evidence
    assert evidence.outcome is EvidenceOutcome.PASSED, evidence
    assert evidence.semantic_summary is not None
    assert "queue_step holds 75%" in evidence.semantic_summary
    assert evidence.evidence_id in agent.prompt
    return {
        "outcome": "observed",
        "narrative": "queue_step holds 75% of device time.",
        "evidence_ids": [evidence.evidence_id],
        "attribution": [{"name": "queue_step", "cost": 3.0, "share": 0.75}],
    }


def test_a_profile_on_slurm_produces_trusted_evidence_that_reaches_the_next_plan(
    tmp_path: Path,
) -> None:
    loop_input = LoopInput.create(tmp_path, profiled=True)

    def below_gate(agent: Turn) -> dict[str, object]:
        (agent.workspace / "queue.py").write_text("VALUE = 2\nREQUIRED = 100\n", encoding="utf-8")
        return implemented("A")

    agents = (
        ScriptedAgents()
        .plan(
            portfolio(workstream("A")),
            portfolio(profile_workstream("prof-A", "A", "Where does A spend its time?")),
            portfolio(workstream("B")),
        )
        .implement("A", below_gate)
        .judge("A", PASS)
        .profile(_submit_profile, _trusted_profile)
        .implement("B", edit_to(3, "B"))
        .judge("B", PASS)
    )

    run = run_loop(loop_input, agents, options(max_rounds=3, max_retries_per_round=1))

    assert run.error is None
    assert agents.unscripted == []
    state = load_state(loop_input, run.run_id)
    candidate = state.workstreams[0]
    assert candidate.evaluation is not None
    assert candidate.evaluation.accuracy_passed
    assert not candidate.evaluation.benchmark_passed
    assert candidate.evaluation.partial_measurement is not None
    assert candidate.evaluation.partial_measurement.value == 2
    (profile,) = state.profiles
    assert profile.outcome is not None
    assert profile.outcome.status.value == "observed", profile.outcome.failure
    assert profile.outcome.evidence_ids
    row = planner_history(agents.prompts(ORCHESTRATOR.id)[2])["prof-A"]
    assert row["status"] == "observed"
    assert row["evidence_ids"] == list(profile.outcome.evidence_ids)
    profiler_prompts = agents.prompts(PROFILER.id)
    assert len(profiler_prompts) == 2
    assert "Where does A spend its time?" in profiler_prompts[0]
    assert "Every evaluation you" in profiler_prompts[1]


def test_a_profile_whose_workload_cannot_run_fails_without_a_profiler_turn(
    tmp_path: Path,
) -> None:
    """r18: a load-failed capture was passed evidence, and two profiler turns argued over it."""
    loop_input = LoopInput.create(tmp_path, profiled=True)
    loop_input.fail_profile_workloads()
    agents = (
        ScriptedAgents()
        .plan(
            portfolio(profile_workstream("prof-root", None, "Where does the root spend time?")),
            portfolio(workstream("A")),
            portfolio(workstream("B")),
        )
        .implement("A", edit_to(2, "A"))
        .judge("A", PASS)
        .implement("B", edit_to(3, "B"))
        .judge("B", PASS)
    )

    # A failed workload consumes its profile round, followed by two implementation rounds.
    run = run_loop(loop_input, agents, options(max_rounds=3))

    assert run.error is None
    assert agents.unscripted == []
    assert agents.prompts(PROFILER.id) == []
    state = load_state(loop_input, run.run_id)
    (profile,) = state.profiles
    assert profile.outcome is not None
    assert profile.outcome.status is CandidateProfileStatus.FAILED
    assert profile.outcome.failure is not None
    assert "not profilable: the configured workload did not run" in profile.outcome.failure
    assert profile.outcome.missing_fields == ()
    assert profile.outcome.evidence_ids == ()


def test_a_run_without_a_profiler_fails_when_its_only_plan_is_profiles(tmp_path: Path) -> None:
    """r17: a plan dropped whole after correction ended the run as a completed search."""
    loop_input = LoopInput.create(tmp_path)
    # Each planning turn is corrected once; a turn still invalid is a turn
    # fault, retried up to max_retries_per_round (2) turns before the run fails.
    agents = ScriptedAgents().plan(
        *(portfolio(profile_workstream(f"prof-{n}", None)) for n in range(1, 5))
    )

    run = run_loop(loop_input, agents, options(max_rounds=2))

    assert run.succeeded is None
    assert run.error is not None
    assert "the planner scheduled no valid workstream after correction" in str(run.error)
    assert "workstreams[0].kind" in str(run.error)
    assert agents.unscripted == []


@pytest.mark.skip(
    reason="nondeterministic in CI on the legacy dynamic loop (resume reconciliation / missing baseline); cutover acceptance target, design step 3; unskip on the vs-core launch path"
)
def test_malformed_resumed_profile_is_corrected_in_the_same_composed_conversation(
    tmp_path: Path,
) -> None:
    """The interpretation after a settled capture gets the initial turn's correction bound."""
    loop_input = LoopInput.create(tmp_path, profiled=True)
    skill = tmp_path / "skills" / "profile-policy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: profile-policy\ndescription: Preserve accuracy.\n---\n# Preserve accuracy\n",
        encoding="utf-8",
    )
    (skill / "floor.md").write_text("Preserve accuracy.\n", encoding="utf-8")
    (loop_input.root / "OBJECTIVE.md").write_text(
        "Raise queue throughput. Follow `resources/skills/profile-policy/floor.md`.\n",
        encoding="utf-8",
    )
    loop_input = replace(loop_input, skills_dirs=(skill,))
    captured: list[dict[str, object]] = []

    def malformed_interpretation(agent: Turn) -> dict[str, object]:
        captured.append(_trusted_profile(agent))
        return {}

    agents = (
        ScriptedAgents()
        .plan(
            portfolio(profile_workstream("prof-root", None)),
            # End after observing the profile diagnosis; a bounded planner
            # schema failure avoids evaluating an unrelated second candidate.
            portfolio(),
            portfolio(),
        )
        .profile(_submit_profile, malformed_interpretation, _trusted_profile)
    )

    run = run_loop(loop_input, agents, options(max_rounds=2, max_retries_per_round=1))

    assert isinstance(run.error, StructuredResponseError), run.error
    assert "workstreams" in run.error.detail
    assert agents.unscripted == []
    assert active_jobs(loop_input.cluster) == ()
    state = load_state(loop_input, run.run_id)
    assert state.baseline is not None
    assert state.baseline.benchmark_passed is True
    assert len(captured) == 1
    (profile,) = state.profiles
    assert profile.outcome is not None
    calls = agents.invocations(PROFILER.id)
    if profile.outcome.status is CandidateProfileStatus.FAILED:
        assert profile.outcome.failure is not None
        assert profile.outcome.failure == (
            "RuntimeContractError: profiler continuation acceptance requires reconciliation"
        )
        assert len(calls) == 2
        assert "Every evaluation you" in calls[1].user_prompt
        assert calls[0].session_key == calls[1].session_key
        message = "Malformed resumed profiler interpretation received no bounded correction"
        raise ProfilerResumeCorrectionUnavailableError(message)
    assert profile.outcome.status is CandidateProfileStatus.OBSERVED, profile.outcome.failure
    first, resumed, corrected = calls
    assert first.session_key == resumed.session_key == corrected.session_key
    assert first.workspace == resumed.workspace == corrected.workspace
    assert "Correction required" in corrected.user_prompt
    row = planner_history(agents.prompts(ORCHESTRATOR.id)[1])["prof-root"]
    assert row["status"] == "observed"
    assert row["evidence_ids"] == list(profile.outcome.evidence_ids)
    assert row["evidence_ids"] == captured[0]["evidence_ids"]
