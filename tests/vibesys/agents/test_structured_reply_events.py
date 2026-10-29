"""A structured turn reports its reply as the role's typed reply, for every role reply model.

The turn's text is the raw JSON the agent wrote; the lifecycle reads it through the role's own
reply model, so a reader gets the reply kind, its schema and its fields as data and parses no
text. A role that may wait answers in an envelope, and the reply is what the envelope holds.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from vibesys.orchestration.dynamic.search.api import dynamic
from vibesys.orchestration.evolve.search.api import evolve
from vibesys.orchestration.issue_queue.models import IssuePerfEvalResult, PerfMetrics
from vibesys.orchestration.issue_queue.search.api import issue_queue
from vibesys.orchestration.multi.search.api import multi
from vibesys.orchestration.single.search.api import single
from vs_agent.api import (
    AgentClient,
    AgentExecutionPolicy,
    AgentSessionSpec,
    AgentTurnRequest,
)
from vs_agent.api.testing import FakeProvider, FakeTurnScript
from vs_core.api import WaitableReply, WaitingForEvaluation
from vs_runtime.api.core import report_turns
from vs_runtime.api.infrastructure import AgentExecutionFinished
from vs_runtime.api.testing import FakeAgentExecutionLifecycleSink
from vs_sim.api.testing import ManualClock, SimThreads

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from vs_core.api.search import Role

_ROLES: tuple[Role, ...] = tuple(
    role for search in (dynamic, evolve, single, multi, issue_queue) for role in search.roles
)

_Schema = dict[str, Any]
_DEPTH = 4
_TREND_FIELDS = (
    "throughput_trend",
    "latency_trend",
    "primary_metric_trend",
    "secondary_metric_trend",
)


def _resolve(schema: _Schema, defs: _Schema) -> _Schema:
    ref = schema.get("$ref")
    return _resolve(defs[ref.rsplit("/", 1)[-1]], defs) if isinstance(ref, str) else schema


def _text(schema: _Schema) -> st.SearchStrategy[Any]:
    if "pattern" in schema:
        return st.from_regex(schema["pattern"], fullmatch=True)
    return st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-",
        min_size=max(schema.get("minLength", 0), 4),
        max_size=schema.get("maxLength", 12),
    )


def _array(schema: _Schema, defs: _Schema, depth: int) -> st.SearchStrategy[Any]:
    floor = schema.get("minItems", 0)
    if depth == 0:
        return st.just([]) if floor == 0 else st.nothing()
    return st.lists(
        _instances(schema.get("items", {}), defs, depth - 1),
        min_size=floor,
        max_size=max(floor, min(schema.get("maxItems", 2), 2)),
        unique=bool(schema.get("uniqueItems")),
    )


def _instances(schema: _Schema, defs: _Schema, depth: int = _DEPTH) -> st.SearchStrategy[Any]:
    """Values the JSON Schema of a reply model allows: the reply models are the one source."""
    schema = _resolve(schema, defs)
    members = schema.get("anyOf") or schema.get("oneOf")
    leaves: dict[str, st.SearchStrategy[Any]] = {
        "string": _text(schema),
        "integer": st.integers(schema.get("minimum", -(10**6)), schema.get("maximum", 10**6)),
        "number": st.floats(
            schema.get("minimum", -1e6), schema.get("maximum", 1e6), allow_nan=False
        ),
        "boolean": st.booleans(),
        "null": st.none(),
    }
    if "enum" in schema:
        return st.sampled_from(schema["enum"])
    if "const" in schema:
        return st.just(schema["const"])
    if members is not None:
        return st.one_of(*(_instances(member, defs, depth) for member in members))
    if schema.get("type") == "array":
        return _array(schema, defs, depth)
    if schema.get("type") == "object" or "properties" in schema:
        return _objects(schema, defs, depth)
    return leaves.get(schema.get("type", ""), st.none())


def _objects(schema: _Schema, defs: _Schema, depth: int) -> st.SearchStrategy[Any]:
    properties: _Schema = schema.get("properties", {})
    required = set(schema.get("required", ()))
    if not properties:
        extra = schema.get("additionalProperties")
        values = (
            _instances(extra, defs, depth - 1)
            if isinstance(extra, dict) and depth > 0
            else st.none()
        )
        return st.dictionaries(
            st.text(alphabet="abcdef", min_size=1, max_size=4), values, max_size=2
        )
    fields = {
        name: _instances(spec, defs, depth - 1)
        for name, spec in properties.items()
        if name in required or depth > 0
    }
    return st.fixed_dictionaries(
        {name: strategy for name, strategy in fields.items() if name in required},
        optional={name: strategy for name, strategy in fields.items() if name not in required},
    )


def _accepts(model: type[BaseModel], value: object) -> bool:
    try:
        model.model_validate_json(json.dumps(value))
    except ValidationError:
        return False
    return True


@st.composite
def _perf_evaluations(draw: st.DrawFn, schema: _Schema, defs: _Schema) -> _Schema:
    """A performance evaluation whose trend fields are the ones its metrics call for.

    The reply model accepts a value only when the trend fields match the metrics' vocabulary,
    which independent optional fields hit about 1% of the time. The trend fields are chosen from
    the metrics instead of filtered for, so nearly every draw is valid.
    """
    value: _Schema = dict(draw(_instances(schema, defs)))
    serving = _accepts(PerfMetrics, value.get("metrics"))
    trends = ("improved", "regressed", "mixed")
    keys = ("throughput_trend", "latency_trend") if serving else ("primary_metric_trend",)
    for name in _TREND_FIELDS:
        value.pop(name, None)
    for name in keys:
        value[name] = draw(st.sampled_from(trends))
    return value


def _replies(model: type[BaseModel]) -> st.SearchStrategy[BaseModel]:
    """Valid replies of ``model``: schema-shaped JSON the model's own validators then accept."""
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    shaped = (
        _perf_evaluations(schema, defs)
        if model is IssuePerfEvalResult
        else _instances(schema, defs)
    )
    return shaped.filter(lambda value: _accepts(model, value)).map(
        lambda value: model.model_validate_json(json.dumps(value))
    )


def _run(
    workspace: Path, role: Role, schema: type[BaseModel], answer: Mapping[str, object]
) -> AgentExecutionFinished:
    lifecycle = FakeAgentExecutionLifecycleSink()
    client = report_turns(
        AgentClient(
            FakeProvider(script=FakeTurnScript((answer,)), threads=SimThreads()),
            clock=ManualClock(),
            threads=SimThreads(),
        ),
        lifecycle,
    )
    spec = AgentSessionSpec(
        role=role.name,
        provider="fake",
        workspace=workspace,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    client.run(
        session_spec=spec,
        turn=AgentTurnRequest(
            message="m", instructions="i", output_schema=schema, invocation_id="turn-1"
        ),
    )
    finished = lifecycle.events[-1]
    assert isinstance(finished, AgentExecutionFinished)
    return finished


@pytest.mark.parametrize("role", _ROLES, ids=lambda role: role.name)
@settings(
    max_examples=8,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(data=st.data())
def test_every_role_reply_is_reported_as_its_typed_reply(
    tmp_path_factory: pytest.TempPathFactory, role: Role, data: st.DataObject
) -> None:
    reply = data.draw(_replies(role.reply))
    schema = role.wire_reply
    answer = reply.model_dump(mode="json")
    if issubclass(schema, WaitableReply):
        answer = {"reply": answer}

    finished = _run(tmp_path_factory.mktemp("workspace"), role, schema, answer)

    assert finished.reply is not None
    assert finished.reply.kind == role.reply.__name__
    assert finished.reply.value == reply.model_dump(mode="json")
    assert finished.reply.json_schema == role.reply.model_json_schema()


def test_a_wait_is_reported_as_the_wait_it_is(tmp_path: Path) -> None:
    role = next(role for role in _ROLES if role.may_wait_for_evaluations)
    answer = {"reply": {"kind": "waiting_for_evaluation", "handles": ["h1"]}}

    finished = _run(tmp_path, role, role.wire_reply, answer)

    assert finished.reply is not None
    assert finished.reply.kind == WaitingForEvaluation.__name__
    assert finished.reply.value == {"kind": "waiting_for_evaluation", "handles": ["h1"]}


def test_a_turn_with_no_reply_model_reports_none(tmp_path: Path) -> None:
    lifecycle = FakeAgentExecutionLifecycleSink()
    client = report_turns(
        AgentClient(
            FakeProvider(script=FakeTurnScript(("plain words",)), threads=SimThreads()),
            clock=ManualClock(),
            threads=SimThreads(),
        ),
        lifecycle,
    )
    client.run(
        session_spec=AgentSessionSpec(
            role="worker",
            provider="fake",
            workspace=tmp_path,
            policy=AgentExecutionPolicy(require_enforcement=False),
        ),
        turn=AgentTurnRequest(message="m", instructions="i", invocation_id="turn-1"),
    )
    finished = lifecycle.events[-1]
    assert isinstance(finished, AgentExecutionFinished)
    assert finished.reply is None
