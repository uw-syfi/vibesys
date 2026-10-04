"""The planner's hypothesis IDs are parsed into one canonical spelling at the plan boundary.

A plan ID becomes a state key, a workspace and Git ref name, and a lookup ID.
An ID with two spellings (``'0 '`` and the ``'0'`` a consumer strips it to)
once crashed the loop when recording its round, so the plan rejects it, naming
the field and the value for the planner's correction turn.
"""

from __future__ import annotations

import asyncio
import unicodedata
from pathlib import Path

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError
from tests.vibesys.orchestration.dynamic._support import (
    INPUT_BASELINE,
    Script,
    baseline_run,
    dynamic_options,
)

from vibesys.orchestration.dynamic import PLUGIN, DynamicState
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, ORCHESTRATOR
from vibesys.orchestration.dynamic.models import PortfolioPlan, planned_id
from vs_runtime.api import AgentId, RunStatus

_ID_LIMIT = TypeAdapter(AgentId).json_schema()["maxLength"]


def _canonical(value: str) -> bool:
    return value == value.strip() and unicodedata.is_normalized("NFC", value)


_IDS = st.text(
    st.characters(categories=("L", "M", "N", "P", "S"), include_characters=" "),
    min_size=1,
    max_size=_ID_LIMIT,
).filter(_canonical)
# A character that gives an ID a second, look-alike or normalized-away spelling.
_DEFECTS = st.sampled_from([" ", "\t", "\n", "\x00", "\u00a0", "\u200b", "\ufeff", "\u2028"])
_ID_SITES = st.sampled_from(
    ("hypothesis_id", "parent_hypothesis_id", "profile_id", "target_hypothesis_id", "update")
)
_INVALID_IDS = st.one_of(
    st.just(""),
    st.text(alphabet="abc", min_size=_ID_LIMIT + 1, max_size=_ID_LIMIT + 100),
    st.text(st.characters(categories=("Cc", "Cf", "Cs", "Co", "Cn")), min_size=1, max_size=10),
    st.text(alphabet="abc", max_size=10).map(lambda value: "e\u0301" + value),
    st.booleans(),
    st.integers(),
    st.lists(st.integers(), max_size=3),
    st.dictionaries(st.text(max_size=5), st.integers(), max_size=3),
)


def _workstream(identifier: str) -> dict[str, object]:
    return {
        "hypothesis_id": identifier,
        "title": "Investigate",
        "hypothesis": "A mechanism limits throughput.",
        "task": "Implement and verify it.",
        "pass_criteria": "Throughput improves.",
    }


def _plan(identifier: str, *, update: str | None = None) -> dict[str, object]:
    updates = (
        []
        if update is None
        else [
            {
                "hypothesis_id": update,
                "disposition": "parked",
                "reason_kind": "lower_priority",
                "reason": "r",
            }
        ]
    )
    return {
        "reasoning": "Independent mechanisms.",
        "workstreams": [_workstream(identifier)],
        "hypothesis_updates": updates,
    }


def _plan_with_id(
    field: str, identifier: object
) -> tuple[dict[str, object], tuple[str | int, ...]]:
    entry = _workstream("H1")
    plan = _plan("H1")
    plan["workstreams"] = [entry]
    if field == "update":
        update = {
            "hypothesis_id": identifier,
            "disposition": "parked",
            "reason_kind": "lower_priority",
            "reason": "Higher priority alternatives exist.",
        }
        plan["hypothesis_updates"] = [update]
        return plan, ("hypothesis_updates", 0, "hypothesis_id")
    if field in {"profile_id", "target_hypothesis_id"}:
        entry = {
            "kind": "profile",
            "profile_id": "P1",
            "target_hypothesis_id": None,
            "question": "Which mechanism dominates?",
            "decision_impact": "Choose the implementation for the dominant mechanism.",
        }
        plan["workstreams"] = [entry]
    entry[field] = identifier
    kind = "profile" if field in {"profile_id", "target_hypothesis_id"} else "implement"
    return plan, ("workstreams", 0, kind, field)


def test_a_trailing_space_id_is_rejected_naming_the_field_and_the_spelling_to_use() -> None:
    with pytest.raises(ValidationError) as rejected:
        PortfolioPlan.model_validate(_plan("0 "))

    message = str(rejected.value)
    assert "workstreams.0.implement.hypothesis_id" in message
    assert "'0 ' is not a valid identifier" in message
    assert "use '0'" in message


@given(identifier=_IDS)
@example(identifier="KV.Cache_v2 / ../Ünïcode")
@example(identifier="H1")
def test_a_canonical_id_reaches_the_plan_and_its_updates_unchanged(identifier: str) -> None:
    plan = PortfolioPlan.model_validate(_plan(identifier, update=identifier))

    assert planned_id(plan.workstreams[0]) == identifier
    assert plan.hypothesis_updates[0].hypothesis_id == identifier


@given(
    identifier=_IDS,
    defect=_DEFECTS,
    edge=st.sampled_from(["start", "end"]),
    field=st.sampled_from(["workstreams", "hypothesis_updates"]),
)
def test_an_id_with_a_second_spelling_is_rejected_where_it_was_written(
    identifier: str, defect: str, edge: str, field: str
) -> None:
    defective = defect + identifier if edge == "start" else identifier + defect
    plan = _plan("H1", update=defective) if field == "hypothesis_updates" else _plan(defective)

    with pytest.raises(ValidationError) as rejected:
        PortfolioPlan.model_validate(plan)

    # A workstream entry is validated as its kind, which the location names.
    location = (
        "workstreams.0.implement.hypothesis_id"
        if field == "workstreams"
        else "hypothesis_updates.0.hypothesis_id"
    )
    assert location in str(rejected.value)
    assert repr(defective) in str(rejected.value)


@given(identifier=_IDS, field=_ID_SITES)
@example(identifier="KV.Cache_v2 / ../Ünïcode", field="profile_id")
@example(identifier="資料 🧪", field="target_hypothesis_id")
def test_every_agent_named_id_roundtrips_with_one_spelling(identifier: str, field: str) -> None:
    payload, _ = _plan_with_id(field, identifier)
    plan = PortfolioPlan.model_validate(payload)
    assert (
        PortfolioPlan.model_validate_json(plan.model_dump_json()).model_dump() == plan.model_dump()
    )
    if field == "update":
        assert plan.hypothesis_updates[0].hypothesis_id == identifier
    else:
        assert getattr(plan.workstreams[0], field) == identifier


@given(identifier=_INVALID_IDS, field=_ID_SITES)
@example(identifier="e\u0301", field="parent_hypothesis_id")
@example(identifier=True, field="profile_id")
def test_malformed_ids_are_typed_rejections_at_every_reference(
    identifier: object, field: str
) -> None:
    payload, path = _plan_with_id(field, identifier)
    with pytest.raises(ValidationError) as rejected:
        PortfolioPlan.model_validate(payload)
    assert any(error["loc"] == path for error in rejected.value.errors())


@settings(max_examples=20)
@given(identifier=_IDS, second_kind=st.sampled_from(("implement", "profile")))
@example(identifier="資料 🧪", second_kind="profile")
def test_repeated_ids_across_kinds_are_corrected_and_never_dispatched_twice(
    identifier: str, second_kind: str
) -> None:
    duplicate = _workstream(identifier)
    if second_kind == "profile":
        duplicate = {
            "kind": "profile",
            "profile_id": identifier,
            "target_hypothesis_id": None,
            "question": "Where does time go?",
            "decision_impact": "Choose the dominant mechanism to implement.",
        }
    invalid = _plan(identifier)
    invalid["workstreams"] = [_workstream(identifier), duplicate]
    next_id = "next" if identifier != "next" else "other"
    script = Script(
        {
            ORCHESTRATOR.id: [invalid, invalid, _plan(next_id)],
            IMPLEMENTER.id: [{"summary": "No viable change.", "outcome": "disproven"}] * 2,
        }
    )

    async def scenario() -> DynamicState:
        run = baseline_run(Path("boundary-workspace"), script)
        run.evaluation.script_root_benchmark(INPUT_BASELINE)
        try:
            assert await PLUGIN.orchestrate(run, dynamic_options()) is RunStatus.SUCCEEDED
            state = await run.state.load(DynamicState)
            assert state is not None
            assert len(run.workspaces.candidates) == 2
            return state
        finally:
            await run.close()

    state = asyncio.run(scenario())
    assert [item.hypothesis_id for item in state.workstreams] == [identifier, next_id]
    assert not state.profiles
    planner_prompts = [message for role, _, message in script.calls if role == ORCHESTRATOR.id]
    assert "workstreams[1]" in planner_prompts[1]
    assert "repeats an earlier entry's ID" in planner_prompts[1]
    assert len([role for role, _, _ in script.calls if role == IMPLEMENTER.id]) == 2
