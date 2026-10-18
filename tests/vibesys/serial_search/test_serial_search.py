"""The policy two serial strategies share: plan acceptance, attribution and the plan view."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.serial_search._host import TERMS

from vibesys.candidates.api import NotConfigured
from vibesys.hypothesis import HypothesisConfig, OrchestratorPlan, rounds
from vibesys.hypothesis.state import HypothesisState
from vibesys.metrics import MetricRow, MetricSpace, Objective
from vibesys.profile_focus import ProfileAttributionError
from vibesys.profile_focus.attribution import parse_attribution
from vibesys.serial_search.api import (
    AttributionConfig,
    Closed,
    LedgerJson,
    RoundDone,
    RoundStart,
    SerialRun,
    accept_plan,
    base_commit,
    bottlenecks_of,
    capture_failure,
    focus_of,
    focus_policy,
    plan_evidence,
    record_round,
)
from vs_core.api import (
    AttemptId,
    AttemptRef,
    CaptureCommandBase,
    RevisionId,
    RevisionRef,
    Settlement,
    SettlementId,
)
from vs_core.api.search import Kept
from vs_runtime.api import CommandResult
from vs_runtime.api.core import CaptureCommandOwner
from vs_runtime.api.testing import FakeCommands, FakeWorkspace, FakeWorkspaces
from vs_sim.api.testing import examples

_CONFIG = HypothesisConfig(max_rounds=4)
_INPUT = RevisionRef.of_git_commit("input")


def _start(number: int = 1) -> RoundStart:
    """Where a new run stands when round ``number`` begins: nothing has passed."""
    return RoundStart(number=number, base=_INPUT, ledger=LedgerJson(), baseline=NotConfigured())


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
        start=_start(3),
    )
    assert isinstance(plan, OrchestratorPlan)
    assert plan.hypothesis_id == (identifier.strip() or "hypothesis-0003")
    assert plan.recommended_skills == []


def test_a_plan_that_updates_itself_twice_or_reuses_an_id_is_refused() -> None:
    ledger = rounds.decode(rounds.EncodedLedger())
    update = {"hypothesis_id": "old", "disposition": "parked", "reason": "defective"}
    duplicate = accept_plan(
        _CONFIG, ledger, _reply("h1", hypothesis_updates=[update, update]), start=_start()
    )
    selfish = accept_plan(
        _CONFIG,
        ledger,
        _reply("h1", hypothesis_updates=[{**update, "hypothesis_id": "h1"}]),
        start=_start(),
    )
    assert "more than once" in str(duplicate)
    assert "new hypothesis" in str(selfish)


@settings(max_examples=examples(pr=20, nightly=200))
@given(
    direction=st.sampled_from(["max", "min"]),
    ratio=st.floats(min_value=0.2, max_value=3.0, allow_nan=False),
)
def test_an_expected_effect_bar_is_accepted_only_on_the_better_side_of_the_base(
    direction: Literal["max", "min"], ratio: float
) -> None:
    """The bar is a ratio over the base in the metric's own direction: never a demanded loss."""
    space = MetricSpace(objectives=(Objective("throughput", direction),))
    ledger = rounds.Ledger(HypothesisState(metrics=space))
    accepted = accept_plan(_CONFIG, ledger, _reply("h1", minimum_ratio=ratio), start=_start())
    asks_a_loss = ratio < 1 if direction == "max" else ratio > 1
    assert isinstance(accepted, str) == asks_a_loss
    if isinstance(accepted, str):
        assert "minimum_ratio" in accepted
        assert direction in accepted


def test_a_run_without_a_headline_metric_has_no_direction_to_hold_a_bar_to() -> None:
    ledger = rounds.Ledger(rounds.decode(rounds.EncodedLedger()).search)
    assert isinstance(
        accept_plan(_CONFIG, ledger, _reply("h1", minimum_ratio=0.5), start=_start()),
        OrchestratorPlan,
    )


def test_a_plan_that_states_no_bar_asks_for_any_gain_beyond_noise() -> None:
    assert _reply("h1").minimum_ratio == 1.0


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


# -- the base a plan names ----------------------------------------------------------------------


def _settlement(number: int, *, eligible: bool) -> Settlement:
    return Settlement.model_validate(
        {
            "settlement_id": SettlementId(root=f"settlement-{number}"),
            "attempt": AttemptRef(attempt_id=AttemptId(root=f"attempt-{number}"), generation=0),
            "candidate": RevisionRef.of_git_commit(f"c{number}"),
            "assessments": (),
            "eligible": eligible,
            "retention": "candidate",
            "outcome": "succeeded",
        }
    )


def _played(drawn: list[tuple[bool, bool, float]]) -> SerialRun:
    """A run that played one round per draw: (passed, eligible if it passed, headline value)."""
    run = SerialRun(
        hypotheses=HypothesisConfig(max_rounds=10),
        input=_INPUT,
        terms=TERMS,
        baseline=NotConfigured(),
    )
    for number, (passed, faster, value) in enumerate(drawn, start=1):
        eligible = passed and faster
        kept = Closed(
            round_number=number,
            hypothesis_id=f"h{number}",
            kept=Kept.of(_settlement(number, eligible=eligible)),
            passed=passed,
            eligible=eligible,
            metrics=(MetricRow(name="throughput", value=value, direction="max", unit="tokens/s"),),
        )
        done = RoundDone(
            number=number,
            ledger=LedgerJson(),
            closed=kept,
            checkpoint=RevisionRef.of_git_commit(f"c{number}"),
        )
        run.finished(done, f"flow:round#{number}")
    return run


_DRAWN = st.lists(st.tuples(st.booleans(), st.booleans(), st.floats(1, 1000)), max_size=6)


@settings(max_examples=examples(pr=10, nightly=100))
@given(drawn=_DRAWN, named=st.none() | st.integers(0, 9))
def test_a_plans_base_is_the_best_the_input_or_a_passed_idea_and_anything_else_is_refused_by_id(
    drawn: list[tuple[bool, bool, float]], named: int | None
) -> None:
    run = _played(drawn)
    start = run.start()
    passed = {item.round_number: item.revision for item in start.passed_ideas()}
    ledger = rounds.decode(rounds.EncodedLedger())
    reply = _reply("next", **({} if named is None else {"base_round": named}))

    accepted = accept_plan(_CONFIG, ledger, reply, start=start)

    if named is None or named == 0 or named in passed:
        assert isinstance(accepted, OrchestratorPlan)
        base = base_commit(accepted, start, input_revision=_INPUT)
        best = run.leader()
        if named is None:
            assert base.revision == (best.revision if isinstance(best, Kept) else _INPUT)
            assert base.round == start.best_round
        elif named == 0:
            assert (base.revision, base.round) == (_INPUT, None)
        else:
            assert (base.revision, base.round) == (passed[named], named)
        failed = {
            RevisionRef.of_git_commit(f"c{number}")
            for number, (ok, _, _) in enumerate(drawn, start=1)
            if not ok
        }
        assert base.revision not in failed
    else:
        assert isinstance(accepted, str)
        assert f"base_round {named} " in accepted
