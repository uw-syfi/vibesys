"""The policy two serial strategies share: plan acceptance, attribution and the plan view."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.hypothesis import HypothesisConfig, OrchestratorPlan, rounds
from vibesys.profile_focus import ProfileAttributionError
from vibesys.profile_focus.attribution import parse_attribution
from vibesys.serial_search.api import (
    AttributionConfig,
    accept_plan,
    bottlenecks_of,
    capture_failure,
    focus_of,
    focus_policy,
    plan_evidence,
    record_round,
)
from vs_core.api import CaptureCommandBase, RevisionId, RevisionRef
from vs_runtime.api import CommandResult
from vs_runtime.api.core import CaptureCommandOwner
from vs_runtime.api.testing import FakeCommands, FakeWorkspace, FakeWorkspaces
from vs_sim.api.testing import examples

_CONFIG = HypothesisConfig(max_rounds=4)


class _Capture(CaptureCommandBase):
    kind: Literal["test.capture_attribution"] = "test.capture_attribution"


def _reply(identifier: str, **extra: object) -> OrchestratorPlan:
    return OrchestratorPlan.model_validate_json(
        json.dumps(
            {
                "hypothesis_id": identifier,
                "title": "a title",
                "hypothesis": "claim",
                "task": "task",
                "pass_criteria": "faster",
                "reasoning": "because",
                **extra,
            }
        )
    )


@settings(max_examples=examples(pr=8, nightly=100))
@given(identifier=st.text(alphabet=" \tabcxyz-0123", max_size=12))
def test_a_valid_plan_gets_a_stripped_nonempty_id_whatever_the_designer_wrote(
    identifier: str,
) -> None:
    plan = accept_plan(
        _CONFIG,
        rounds.Ledger(rounds.decode(rounds.EncodedLedger()).search),
        _reply(identifier),
        round_number=3,
    )
    assert isinstance(plan, OrchestratorPlan)
    assert plan.hypothesis_id == (identifier.strip() or "hypothesis-0003")
    assert plan.recommended_skills == []


def test_a_plan_that_updates_itself_twice_or_reuses_an_id_is_refused() -> None:
    ledger = rounds.decode(rounds.EncodedLedger())
    update = {"hypothesis_id": "old", "disposition": "parked", "reason": "defective"}
    duplicate = accept_plan(
        _CONFIG, ledger, _reply("h1", hypothesis_updates=[update, update]), round_number=1
    )
    selfish = accept_plan(
        _CONFIG,
        ledger,
        _reply("h1", hypothesis_updates=[{**update, "hypothesis_id": "h1"}]),
        round_number=1,
    )
    assert "more than once" in str(duplicate)
    assert "new hypothesis" in str(selfish)


def test_attribution_output_that_is_not_the_protocol_is_refused() -> None:
    with pytest.raises(ProfileAttributionError):
        bottlenecks_of("not json")


def test_attribution_output_past_the_size_cap_is_refused_before_it_is_parsed() -> None:
    padding = " " * (16 * 1024 * 1024)
    framed = f'__VIBESYS_ATTRIBUTION_BEGIN__{{"version": 1,{padding}"components": []}}__VIBESYS_ATTRIBUTION_END__'
    with pytest.raises(ProfileAttributionError, match="larger than"):
        parse_attribution(framed)


async def test_failed_capture_attribution_never_displays_raw_secret_fragments() -> None:
    """The public operation passes only its complete-stream diagnostic to attribution."""
    revision = RevisionRef(revision_id=RevisionId(root="r1"), digest="git-commit:r1")
    workspaces = FakeWorkspaces(
        FakeWorkspace(path=Path("/workspace"), known_revisions={"r1"}),
        supports_parallel_candidates=True,
    )
    commands = FakeCommands()
    secret = "s" * 240
    commands.script(
        CommandResult(
            output=f"prefix API_TOKEN={secret[:20]}...[truncated]...{secret[-20:]} tail",
            exit_code=7,
            truncated=True,
            diagnostic_output="prefix API_TOKEN=[REDACTED] tail",
        )
    )
    owner = CaptureCommandOwner(
        workspaces,
        commands,
        workspaces,
        lambda ref: ref.revision_id.root if ref == revision else None,
    )

    capture = await owner.capture(
        _Capture(
            revision=revision,
            argv=("bash", "attribute.sh"),
            output_argument="--out",
            timeout_seconds=30,
        ),
        "operation-id",
    )
    detail = capture_failure(
        ("bash", "attribute.sh"),
        exit_code=cast("int", capture["exit_code"]),
        detail="",
        output=cast("str", capture["output"]),
    )

    assert "API_TOKEN=[REDACTED]" in detail
    assert secret[:20] not in detail
    assert secret[-20:] not in detail


def test_a_plain_run_has_no_focus_policy_and_records_nothing() -> None:
    ledger = rounds.decode(rounds.EncodedLedger())
    assert focus_policy(None) is None
    assert focus_of(ledger, None) is None
    assert record_round(ledger, None, passed=True) is ledger


def test_a_profile_guided_run_has_a_policy_from_its_thresholds() -> None:
    policy = focus_policy(AttributionConfig(command=("profile",), min_measured_rounds=3))
    assert policy is not None
    assert focus_of(rounds.decode(rounds.EncodedLedger()), policy) is None


def test_the_plan_view_of_a_new_run_shows_no_history() -> None:
    ledger = rounds.decode(rounds.EncodedLedger())
    planning = rounds.planning(_CONFIG, ledger, 1)
    evidence = plan_evidence(_CONFIG, ledger, planning, None)
    assert (evidence.round_number, evidence.max_rounds) == (1, 4)
    assert evidence.history == ()
    assert evidence.frontier == ()
    assert evidence.older_rounds == 0
