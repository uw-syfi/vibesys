"""Planner decisions declare the intent of each slot before work is dispatched."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.vibesys.orchestration.dynamic._support import (
    INPUT_BASELINE,
    Script,
    baseline_run,
    dynamic_options,
)

from vibesys.orchestration.dynamic import (
    PLUGIN,
    DynamicPlanningError,
    DynamicState,
    EvidenceReference,
    ImplementPortfolioPlan,
    PortfolioPlan,
    ProfileDecision,
    WorkstreamPlan,
)
from vibesys.orchestration.dynamic.agents import ORCHESTRATOR
from vs_runtime.api import StructuredResponseError

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_runtime.api import AgentRole


_R21_PROFILE: dict[str, object] = {
    "kind": "profile",
    "profile_id": "prefix_cache_correctness_and_reuse",
    "question": "Is this unexpectedly a profile kind?",
    "target_hypothesis_id": None,
}
_IMPLEMENT: dict[str, object] = {
    "kind": "implement",
    "hypothesis_id": "prefix-cache",
    "title": "Exact-prefix cache",
    "hypothesis": "Reusing exact-prefix state removes repeated prefill.",
    "task": "Implement exact-prefix reuse and test cache correctness.",
    "pass_criteria": "Correct outputs and lower repeated-prefix latency.",
}
_PROFILE: dict[str, object] = {
    "kind": "profile",
    "profile_id": "prefill-cost",
    "target_hypothesis_id": None,
    "question": "How much latency is spent recomputing repeated prefixes?",
    "decision_impact": "Choose prefix caching if repeated prefill dominates latency.",
}
_REQUIRED = {
    "implement": ("hypothesis_id", "title", "hypothesis", "task", "pass_criteria"),
    "profile": ("profile_id", "target_hypothesis_id", "question", "decision_impact"),
}
_TEXT = st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=1, max_size=80).filter(
    lambda value: bool(value.strip())
)
_UNICODE_TEXT = st.text(min_size=1, max_size=12000).filter(lambda value: bool(value.strip()))
_BLANK = st.text(alphabet=" \t\n\r\u00a0\u2003\u2028\u3000", max_size=30)
_JSON_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=40),
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=10), children, max_size=3)
    ),
    max_leaves=10,
)
_CAPPED_FIELDS = (
    (
        "profile",
        "question",
        ProfileDecision.model_json_schema()["properties"]["question"]["maxLength"],
    ),
    *(
        (
            "evidence",
            field,
            next(
                alternative["maxLength"]
                for alternative in details.get("anyOf", [details])
                if "maxLength" in alternative
            ),
        )
        for field, details in EvidenceReference.model_json_schema()["properties"].items()
    ),
)


def _plan(entry: dict[str, object]) -> dict[str, object]:
    return {"reasoning": "Reserve slots according to their intended work.", "workstreams": [entry]}


def test_r21_implementation_disguised_as_a_profile_lacks_measurement_intent() -> None:
    with pytest.raises(ValidationError) as rejected:
        PortfolioPlan.model_validate(_plan(_R21_PROFILE))

    assert rejected.value.errors()[0]["loc"] == ("workstreams", 0, "profile", "decision_impact")
    assert rejected.value.errors()[0]["type"] == "missing"


@given(kind=st.sampled_from(tuple(_REQUIRED)), text=_TEXT, data=st.data())
def test_each_kind_requires_its_intent_fields(kind: str, text: str, data: st.DataObject) -> None:
    entry: dict[str, object] = dict(_IMPLEMENT if kind == "implement" else _PROFILE)
    for field in _REQUIRED[kind]:
        if field not in {"hypothesis_id", "profile_id", "target_hypothesis_id"}:
            entry[field] = text
    parsed = PortfolioPlan.model_validate(_plan(entry))
    assert PortfolioPlan.model_validate_json(parsed.model_dump_json()) == parsed

    field = data.draw(st.sampled_from(_REQUIRED[kind]))
    entry.pop(field)
    with pytest.raises(ValidationError) as rejected:
        PortfolioPlan.model_validate(_plan(entry))
    assert any(
        error["loc"] == ("workstreams", 0, kind, field) and error["type"] == "missing"
        for error in rejected.value.errors()
    )


@given(
    kind=st.sampled_from(tuple(_REQUIRED)),
    data=st.data(),
)
def test_variant_fields_cannot_contradict_the_declared_kind(kind: str, data: st.DataObject) -> None:
    entry: dict[str, object] = dict(_IMPLEMENT if kind == "implement" else _PROFILE)
    other = _PROFILE if kind == "implement" else _IMPLEMENT
    field = data.draw(st.sampled_from(tuple(key for key in other if key != "kind")))
    entry[field] = other[field]

    with pytest.raises(ValidationError) as rejected:
        PortfolioPlan.model_validate(_plan(entry))

    assert any(
        error["loc"] == ("workstreams", 0, kind, field) and error["type"] == "extra_forbidden"
        for error in rejected.value.errors()
    )


@given(
    kind=st.sampled_from(tuple(_REQUIRED)),
    blank=_BLANK,
    data=st.data(),
)
def test_blank_intent_is_rejected_at_the_field(kind: str, blank: str, data: st.DataObject) -> None:
    entry: dict[str, object] = dict(_IMPLEMENT if kind == "implement" else _PROFILE)
    fields = tuple(
        field
        for field in _REQUIRED[kind]
        if field not in {"hypothesis_id", "profile_id", "target_hypothesis_id"}
    )
    field = data.draw(st.sampled_from(fields))
    entry[field] = blank

    with pytest.raises(ValidationError) as rejected:
        PortfolioPlan.model_validate(_plan(entry))

    assert any(error["loc"] == ("workstreams", 0, kind, field) for error in rejected.value.errors())


@given(
    choice=st.sampled_from(
        tuple(
            (kind, field)
            for kind, fields in _REQUIRED.items()
            for field in fields
            if field not in {"hypothesis_id", "profile_id", "target_hypothesis_id", "question"}
        )
    ),
    text=_UNICODE_TEXT,
)
@example(choice=("implement", "task"), text="界🧪" * 5000)
@example(choice=("profile", "decision_impact"), text="界🧪" * 5000)
def test_free_text_roundtrips_without_truncation(choice: tuple[str, str], text: str) -> None:
    kind, field = choice
    entry = dict(_IMPLEMENT if kind == "implement" else _PROFILE)
    entry[field] = text
    parsed = PortfolioPlan.model_validate(_plan(entry))
    restored = PortfolioPlan.model_validate_json(parsed.model_dump_json())

    assert getattr(restored.workstreams[0], field) == text


@given(
    key=st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=40).map(
        lambda key: "unknown_" + key
    ),
    value=_JSON_VALUES,
    location=st.sampled_from(("portfolio", "implement", "profile", "evidence", "update")),
)
def test_unknown_keys_are_rejected_at_every_plan_boundary(
    key: str, value: object, location: str
) -> None:
    entry = dict(_PROFILE if location == "profile" else _IMPLEMENT)
    plan = _plan(entry)
    target: dict[str, object] = plan
    path: tuple[str | int, ...] = ()
    if location in {"implement", "profile"}:
        target = entry
        path = ("workstreams", 0, location)
    elif location == "evidence":
        target = {"location": "candidate.py", "purpose": "implementation evidence"}
        entry["evidence"] = [target]
        path = ("workstreams", 0, "implement", "evidence", 0)
    elif location == "update":
        target = {
            "hypothesis_id": "prior",
            "disposition": "parked",
            "reason_kind": "lower_priority",
            "reason": "Other mechanisms have higher priority.",
        }
        plan["hypothesis_updates"] = [target]
        path = ("hypothesis_updates", 0)
    target[key] = value

    with pytest.raises(ValidationError) as rejected:
        PortfolioPlan.model_validate(plan)

    assert any(
        error["loc"] == (*path, key) and error["type"] == "extra_forbidden"
        for error in rejected.value.errors()
    )


@given(blank=_BLANK)
@example(blank=" ")
def test_blank_portfolio_reasoning_is_rejected_at_its_field(blank: str) -> None:
    plan = _plan(dict(_IMPLEMENT))
    plan["reasoning"] = blank

    with pytest.raises(ValidationError) as rejected:
        PortfolioPlan.model_validate(plan)

    assert any(error["loc"] == ("reasoning",) for error in rejected.value.errors())


@given(blank=_BLANK)
def test_blank_strategy_update_reason_is_rejected_at_its_field(blank: str) -> None:
    payload = _plan(dict(_IMPLEMENT))
    payload["hypothesis_updates"] = [
        {
            "hypothesis_id": "prior",
            "disposition": "parked",
            "reason_kind": "lower_priority",
            "reason": blank,
        }
    ]
    with pytest.raises(ValidationError) as rejected:
        PortfolioPlan.model_validate(payload)
    assert any(
        error["loc"] == ("hypothesis_updates", 0, "reason") for error in rejected.value.errors()
    )


@given(blank=_BLANK, field=st.sampled_from(("location", "purpose", "revision")))
@example(blank=" ", field="location")
@example(blank=" ", field="purpose")
@example(blank=" ", field="revision")
def test_blank_evidence_fields_are_rejected_at_the_cited_field(blank: str, field: str) -> None:
    evidence = {"location": "candidate.py", "purpose": "implementation evidence", "revision": "r1"}
    evidence[field] = blank
    entry = dict(_IMPLEMENT)
    entry["evidence"] = [evidence]

    with pytest.raises(ValidationError) as rejected:
        PortfolioPlan.model_validate(_plan(entry))

    assert any(
        error["loc"] == ("workstreams", 0, "implement", "evidence", 0, field)
        for error in rejected.value.errors()
    )


@given(text=_UNICODE_TEXT)
@example(text="根拠🧪" * 5000)
def test_portfolio_reasoning_roundtrips_without_truncation(text: str) -> None:
    payload = _plan(dict(_IMPLEMENT))
    payload["reasoning"] = text
    plan = PortfolioPlan.model_validate(payload)
    assert PortfolioPlan.model_validate_json(plan.model_dump_json()).reasoning == text


@given(
    choice=st.sampled_from(_CAPPED_FIELDS),
    offset=st.integers(min_value=-1, max_value=2),
)
def test_capped_fields_reject_overlong_text_at_the_exact_path(
    choice: tuple[str, str, int], offset: int
) -> None:
    kind, field, limit = choice
    text = "界" * (limit + offset)
    entry = dict(_PROFILE if kind == "profile" else _IMPLEMENT)
    if kind == "profile":
        entry[field] = text
        path = ("workstreams", 0, "profile", field)
    else:
        evidence = {
            "location": "candidate.py",
            "purpose": "The candidate change.",
            "revision": "r1",
        }
        evidence[field] = text
        entry["evidence"] = [evidence]
        path = ("workstreams", 0, "implement", "evidence", 0, field)
    if offset > 0:
        with pytest.raises(ValidationError) as rejected:
            PortfolioPlan.model_validate(_plan(entry))
        assert any(
            error["loc"] == path and error["type"] == "string_too_long"
            for error in rejected.value.errors()
        )
    else:
        parsed = PortfolioPlan.model_validate_json(
            PortfolioPlan.model_validate(_plan(entry)).model_dump_json()
        )
        planned = parsed.workstreams[0]
        if kind == "profile":
            assert isinstance(planned, ProfileDecision)
            restored = planned
        else:
            assert isinstance(planned, WorkstreamPlan)
            restored = planned.evidence[0]
        assert getattr(restored, field) == text


@given(kind=_JSON_VALUES.filter(lambda value: value not in tuple(_REQUIRED)))
def test_unknown_workstream_kinds_are_typed_rejections(kind: object) -> None:
    entry = dict(_IMPLEMENT)
    entry["kind"] = kind
    with pytest.raises(ValidationError) as rejected:
        PortfolioPlan.model_validate(_plan(entry))
    assert any(
        error["loc"] == ("workstreams", 0) and error["type"] == "union_tag_invalid"
        for error in rejected.value.errors()
    )


@settings(max_examples=20)
@given(
    key=st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=30), value=_JSON_VALUES
)
def test_repeated_malformed_replies_end_in_typed_failure_without_dispatch(
    key: str, value: object
) -> None:
    offending = "unknown_" + key
    reply = _plan(dict(_IMPLEMENT))
    reply[offending] = value
    script = Script({ORCHESTRATOR.id: [reply, reply]})

    async def scenario() -> None:
        run = baseline_run(Path("boundary-workspace"), script)
        run.evaluation.script_root_benchmark(INPUT_BASELINE)
        try:
            with pytest.raises(StructuredResponseError, match=offending):
                await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1))
            state = await run.state.load(DynamicState)
            assert state is None or (not state.workstreams and not state.profiles)
            assert not run.workspaces.candidates
            assert all(session.closed for session in run.agents.sessions)
        finally:
            await run.close()

    asyncio.run(scenario())
    assert [role for role, _, _ in script.calls] == [ORCHESTRATOR.id, ORCHESTRATOR.id]
    assert script.histories[0][1] == ()
    assert script.histories[1][1] == (script.calls[0][2],)


@settings(max_examples=20)
@given(question=_TEXT, impact=_TEXT)
def test_unavailable_profile_capability_is_absent_from_schema_and_never_dispatched(
    question: str, impact: str
) -> None:
    entry = {**_PROFILE, "question": question, "decision_impact": impact}
    reply = _plan(entry)
    script = Script({ORCHESTRATOR.id: [reply, reply]})
    schema = ImplementPortfolioPlan.model_json_schema()
    assert "ProfileDecision" not in schema["$defs"]

    async def scenario() -> None:
        run = baseline_run(Path("boundary-workspace"), script)
        run.evaluation.script_root_benchmark(INPUT_BASELINE)
        try:
            with pytest.raises(DynamicPlanningError, match=r"workstreams\[0\]\.kind"):
                await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1))
            state = await run.state.load(DynamicState)
            assert state is None or (not state.workstreams and not state.profiles)
            assert not run.workspaces.candidates
        finally:
            await run.close()

    asyncio.run(scenario())
    assert [role for role, _, _ in script.calls] == [ORCHESTRATOR.id, ORCHESTRATOR.id]


@given(revisions=st.lists(_TEXT, min_size=2, max_size=2, unique=True))
def test_evidence_binding_rejects_every_mismatched_revision(revisions: list[str]) -> None:
    reference = EvidenceReference(
        location="eval_measurement", purpose="rate", revision=revisions[0]
    )
    assert reference.with_revision(revisions[0]) == reference
    with pytest.raises(ValueError, match="does not match measured revision"):
        reference.with_revision(revisions[1])
    assert reference.model_copy(update={"revision": None}).with_revision(revisions[0]) == reference


@given(
    revisions=st.lists(_TEXT, min_size=2, max_size=2, unique=True),
    reference_kind=st.sampled_from(("evaluation", "evidence", "profiler")),
)
@settings(max_examples=10)
def test_planner_rejects_mismatched_measurement_references_before_dispatch(
    revisions: list[str],
    reference_kind: str,
) -> None:
    handle = "eval_measurement"
    location = {
        "evaluation": handle,
        "evidence": "a" * 64,
        "profiler": "profiler-measurement",
    }[reference_kind]
    entry = dict(_IMPLEMENT)
    entry["evidence"] = [
        {"location": location, "purpose": "Measured rate", "revision": revisions[1]}
    ]
    reply = _plan(entry)
    script = Script({ORCHESTRATOR.id: [reply, reply]})

    async def scenario() -> None:
        run = baseline_run(Path("attribution-workspace"), script)
        run.evaluation.submitted_revisions[handle] = revisions[0]
        if reference_kind == "evidence":
            run.evaluation.accepted_evidence[handle] = (location,)
        elif reference_kind == "profiler":
            run.evaluation.profiler_revisions[location] = revisions[0]
        run.evaluation.script_root_benchmark(INPUT_BASELINE)
        try:
            with pytest.raises(DynamicPlanningError, match="does not match measured revision"):
                await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1))
            state = await run.state.load(DynamicState)
            assert state is None or not state.workstreams
            assert not run.workspaces.candidates
        finally:
            await run.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("mismatch", [False, True])
def test_planner_validates_measurements_submitted_during_its_turn(*, mismatch: bool) -> None:
    handle = "eval_during_planning"
    entry = dict(_IMPLEMENT)
    entry["evidence"] = [
        {
            "location": handle,
            "purpose": "Fresh measurement",
            "revision": "wrong" if mismatch else "measured",
        }
    ]
    calls = 0

    async def scenario() -> None:
        def respond(
            role: AgentRole,
            _history: tuple[str, ...],
            _message: str,
            _response: type[BaseModel] | None,
        ) -> object:
            nonlocal calls
            if role.id == ORCHESTRATOR.id:
                calls += 1
                run.evaluation.submitted_revisions[handle] = "measured"
                return _plan(entry)
            if role.id == "implementer":
                return {"summary": "Implemented", "outcome": "nominated", "evidence": []}
            return {"passed": True, "analysis": "Correct"}

        run = baseline_run(Path("fresh-attribution-workspace"), Script({}), responder=respond)
        run.evaluation.script_root_benchmark(INPUT_BASELINE)
        try:
            if mismatch:
                with pytest.raises(DynamicPlanningError, match="does not match measured revision"):
                    await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1))
                assert not run.workspaces.candidates
            else:
                await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1))
                state = await run.state.load(DynamicState)
                assert state is not None
                assert state.workstreams[0].plan.evidence[0].revision == "measured"
        finally:
            await run.close()

    asyncio.run(scenario())
    assert calls == (2 if mismatch else 1)
