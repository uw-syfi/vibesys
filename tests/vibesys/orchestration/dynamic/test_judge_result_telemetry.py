"""The judge/review assessment of each workstream attempt reaches `Observations`.

Every `_assess` pass through an implemented candidate reports exactly one
`judge_result`: a verdict when a review ran, or a skip reason when it did not.
`FakeObservations` is this codebase's existing orchestration-layer Fake (see
`vs_runtime.api.testing`); these tests drive the real plugin end to end
through `PLUGIN.orchestrate` and inspect what it published, instead of
reaching into `Workstreams`' private methods.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from tests.vibesys.orchestration.dynamic._support import (
    Script,
    baseline_run,
    dynamic_options,
    implementation,
    portfolio,
)

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR

if TYPE_CHECKING:
    from pathlib import Path


def test_judge_result_reports_pass_when_review_passes(tmp_path: Path) -> None:
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("cache")],
            IMPLEMENTER.id: [implementation("cache")],
            JUDGE.id: [{"passed": True, "analysis": "Candidate is correct."}],
        }
    )
    fake = baseline_run(tmp_path, script)
    asyncio.run(
        PLUGIN.orchestrate(
            fake,
            dynamic_options(max_in_flight=1, max_rounds=1, max_retries_per_round=1, judge_every=1),
        )
    )
    [result] = fake.observations.judge_results
    assert result.round_label == "round-1"
    assert result.verdict == "pass"
    assert result.analysis == "Candidate is correct."
    assert result.feedback == ""
    assert result.skip_reason is None
    assert result.implementer_outcome == "nominated"


def test_judge_result_reports_fail_when_review_fails(tmp_path: Path) -> None:
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("cache")],
            IMPLEMENTER.id: [implementation("cache")],
            JUDGE.id: [
                {"passed": False, "analysis": "Missing coverage.", "feedback": "Add a test."}
            ],
        }
    )
    fake = baseline_run(tmp_path, script)
    asyncio.run(
        PLUGIN.orchestrate(
            fake,
            dynamic_options(max_in_flight=1, max_rounds=1, max_retries_per_round=1, judge_every=1),
        )
    )
    [result] = fake.observations.judge_results
    assert result.round_label == "round-1"
    assert result.verdict == "fail"
    assert result.analysis == "Missing coverage."
    assert result.feedback == "Add a test."
    assert result.skip_reason is None
    assert result.implementer_outcome == "nominated"


def test_judge_result_reports_blocked_skip_reason(tmp_path: Path) -> None:
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("held")],
            IMPLEMENTER.id: [{**implementation("held"), "outcome": "blocked"}],
        }
    )
    fake = baseline_run(tmp_path, script)
    asyncio.run(
        PLUGIN.orchestrate(
            fake,
            dynamic_options(max_in_flight=1, max_rounds=1, max_retries_per_round=1, judge_every=1),
        )
    )
    [result] = fake.observations.judge_results
    assert result.verdict == "skipped"
    assert result.skip_reason == "blocked"
    assert result.analysis == ""
    assert result.feedback == ""
    assert result.implementer_outcome == "blocked"


def test_judge_result_reports_unchanged_skip_reason_before_blocked_or_not_due(
    tmp_path: Path,
) -> None:
    """`unchanged` wins over `not_due` when a tree-unchanged attempt is also not due.

    `_maybe_review` checks `blocked` first, then `unchanged`, then `not_due`;
    this attempt's outcome is `continue` (not blocked, not promotable, and its
    sequence does not land on a `judge_every` boundary, so it would otherwise
    be `not_due`), but the candidate workspace reports no tree change, so the
    workstream's own correlation reports `unchanged` instead.
    """
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("probe")],
            IMPLEMENTER.id: [
                {
                    "summary": "Still exploring the mechanism.",
                    "outcome": "continue",
                    "next_step": "Keep investigating.",
                }
            ],
        }
    )
    fake = baseline_run(tmp_path, script)
    # One implementer attempt snapshots its workspace three times (bracketing
    # the agent turn, then once more after it): all three report no change,
    # matching a real Git-backed workspace whose tree never moved.
    fake.workspaces.script_candidate_unchanged_snapshot(3)
    asyncio.run(
        PLUGIN.orchestrate(
            fake,
            dynamic_options(max_in_flight=1, max_rounds=1, max_retries_per_round=1, judge_every=3),
        )
    )
    [result] = fake.observations.judge_results
    assert result.verdict == "skipped"
    assert result.skip_reason == "unchanged"
    assert result.implementer_outcome == "continue"


def test_judge_result_reports_not_due_skip_reason(tmp_path: Path) -> None:
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("probe")],
            IMPLEMENTER.id: [
                {
                    "summary": "Still exploring the mechanism.",
                    "outcome": "continue",
                    "next_step": "Keep investigating.",
                }
            ],
        }
    )
    fake = baseline_run(tmp_path, script)
    asyncio.run(
        PLUGIN.orchestrate(
            fake,
            dynamic_options(max_in_flight=1, max_rounds=1, max_retries_per_round=1, judge_every=1),
        )
    )
    [result] = fake.observations.judge_results
    assert result.verdict == "skipped"
    assert result.skip_reason == "not_due"
    assert result.implementer_outcome == "continue"
