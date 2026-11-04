"""A planner that parks or abandons a finished hypothesis leaves a record the run can keep.

Regression for #1594: the search stored the update's disposition as the raw string in the
hypothesis record, and the run died revalidating its state on the next commit. The run now
publishes its record as JSON after every step (and a restart replays it), so the record that
holds a retirement must validate and round-trip whatever the planner's reason.
"""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._host import (
    PLANNER_LABEL,
    Host,
    begin,
    config,
)
from tests.vibesys.orchestration.dynamic._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.search_faults import assert_flow_closed
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.hypothesis import HypothesisStrategy
from vibesys.orchestration.dynamic.models import (
    ImplementerResult,
    ReviewResult,
    StrategyReason,
)
from vibesys.orchestration.dynamic.search.api import RunSnapshot
from vs_core.api import Finish, Retained, RevisionRef, StartAttempt

MODES = across_restarts


def retiring_plan(disposition: str, reason_kind: str) -> str:
    """A plan that retires h1 and starts h2."""
    return json.dumps(
        {
            "reasoning": "retire h1, try h2",
            "workstreams": [implement("h2")],
            "hypothesis_updates": [
                {
                    "hypothesis_id": "h1",
                    "disposition": disposition,
                    "reason_kind": reason_kind,
                    "reason": "no further progress",
                }
            ],
            "handoff": {"expectations": [], "doubts": []},
        }
    )


def finish_a_round(host: Host, candidate: str) -> None:
    """The one open workstream ends its round inconclusive, with a candidate the judge passes."""
    (asked,) = host.open(StartAttempt)
    host.answer(asked)
    result = ImplementerResult.model_validate_json(
        implemented("inconclusive", next_step="try the next kernel")
    )
    host.run.reply(
        host.run.expect_turn(role="implementer"),
        result,
        checkpoint=Retained(revision=RevisionRef.of_git_commit(candidate)),
    )
    host.run.reply(host.run.expect_turn(role="judge"), ReviewResult.model_validate_json(reviewed()))


@MODES
@settings(max_examples=2 * len(StrategyReason), deadline=None, derandomize=True, database=None)
@given(
    disposition=st.sampled_from([HypothesisStrategy.PARKED, HypothesisStrategy.ABANDONED]),
    reason_kind=st.sampled_from(list(StrategyReason)),
)
def test_every_retirement_the_planner_schema_allows_leaves_a_record_the_run_keeps_and_publishes(
    *, restarting: bool, disposition: HypothesisStrategy, reason_kind: StrategyReason
) -> None:
    host = begin(config(max_rounds=2), restarting=restarting)
    host.drain_until_turn(PLANNER_LABEL)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(implement("h1")))
    finish_a_round(host, "c1")
    host.drain_until_turn(PLANNER_LABEL)

    host.run.reply_raw(
        host.run.expect_turn(role=PLANNER_LABEL),
        retiring_plan(disposition.value, reason_kind.value),
    )
    record = host.record
    assert record is not None
    retired = next(item for item in record.hypotheses if item.hypothesis_id == "h1")
    assert retired.strategy is disposition
    assert retired.reason_kind is reason_kind
    assert RunSnapshot.model_validate(record.model_dump(mode="python")) == record
    assert RunSnapshot.model_validate_json(record.model_dump_json()) == record

    (asked,) = host.open(StartAttempt)  # the plan that retired h1 still starts h2
    host.answer(asked)
    result = ImplementerResult.model_validate_json(implemented())
    host.run.reply(
        host.run.expect_turn(role="implementer"),
        result,
        checkpoint=Retained(revision=RevisionRef.of_git_commit("c2")),
    )
    host.run.reply(host.run.expect_turn(role="judge"), ReviewResult.model_validate_json(reviewed()))
    host.drain()

    finish = host.run.finished
    assert isinstance(finish, Finish)
    assert finish.result.outcome == "success"
    after = host.record
    assert after is not None
    assert {item.hypothesis_id: item.strategy for item in after.hypotheses}["h1"] is disposition
    assert_flow_closed(host)
