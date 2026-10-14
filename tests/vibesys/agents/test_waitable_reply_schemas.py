"""Waitable role replies stay native on Codex and accept pre-envelope results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from vibesys.hypothesis import HypothesisOutcome
from vibesys.orchestration.dynamic.search.api import dynamic
from vibesys.orchestration.evolve.search.api import evolve
from vibesys.orchestration.issue_queue.core_policy import prompts as issue_queue_prompts
from vibesys.orchestration.issue_queue.search.api import issue_queue
from vibesys.orchestration.multi.search.api import multi
from vibesys.orchestration.review import Verdict
from vibesys.orchestration.single.search.api import single
from vibesys.prompts import render_template
from vs_agent.api import AgentClient, native_schema_problems
from vs_agent.api.testing import FakeExecutor, fake_agentshim_launcher, scripted_turn
from vs_core.api import WaitableReply
from vs_prompts.api import TemplateRenderer
from vs_sim.api import OsThreads
from vs_sim.api.testing import ManualClock

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_core.api.search import Role


_DYNAMIC = {role.name: dynamic.reply_schemas[role.reply_schema] for role in dynamic.roles}
_EVOLVE = {role.name: evolve.reply_schemas[role.reply_schema] for role in evolve.roles}
_SINGLE = {role.name: single.reply_schemas[role.reply_schema] for role in single.roles}
_MULTI = {role.name: multi.reply_schemas[role.reply_schema] for role in multi.roles}
_ISSUE_QUEUE = {
    role.name: issue_queue.reply_schemas[role.reply_schema] for role in issue_queue.roles
}

_IMPLEMENTER = {"summary": "done", "outcome": HypothesisOutcome.SUPPORTED}
_JUDGE = {"passed": True, "analysis": "checked"}
_SINGLE_RESULT = {
    "summary": "done",
    "expected_behavior": "faster",
    "self_review": "checked",
    "feedback": "",
    "verdict": Verdict.PASS,
    "bottlenecks": "",
    "suggestions": "",
    "profile_analysis": "",
}
_MULTI_IMPLEMENTER = {"summary": "done", "expected_behavior": "faster"}
_EVOLVE_MUTATOR = {"summary": "did it", "hypothesis": "because", "expected_behavior": "faster"}
_EVOLVE_JUDGE = {"analysis": "checked", "feedback": "", "verdict": "pass"}
_MULTI_JUDGE = {"analysis": "checked", "feedback": "", "verdict": Verdict.PASS}
_ISSUE_QUEUE_IMPLEMENTER = {
    "summary": "done",
    "files_touched": (),
    "self_check": "checked",
}
_ISSUE_QUEUE_JUDGE = {
    "analysis": "checked",
    "feedback": "",
    "verdict": "pass",
    "new_issues": (),
}
_AFFECTED_ROLES: tuple[tuple[str, type[BaseModel], dict[str, object]], ...] = (
    ("dynamic-implementer", _DYNAMIC["implementer"], _IMPLEMENTER),
    ("dynamic-judge", _DYNAMIC["judge"], _JUDGE),
    ("evolve-mutator", _EVOLVE["mutator"], _EVOLVE_MUTATOR),
    ("evolve-judge", _EVOLVE["judge"], _EVOLVE_JUDGE),
    ("single-implementer", _SINGLE["implementer"], _SINGLE_RESULT),
    ("multi-implementer", _MULTI["implementer"], _MULTI_IMPLEMENTER),
    ("multi-judge", _MULTI["judge"], _MULTI_JUDGE),
    (
        "issue-queue-implementer",
        _ISSUE_QUEUE["implementer"],
        _ISSUE_QUEUE_IMPLEMENTER,
    ),
    ("issue-queue-judge", _ISSUE_QUEUE["judge"], _ISSUE_QUEUE_JUDGE),
)


@pytest.mark.parametrize(
    ("_role", "model", "bare"), _AFFECTED_ROLES, ids=[role for role, _, _ in _AFFECTED_ROLES]
)
def test_pre_envelope_role_replies_remain_compatible(
    _role: str, model: type[BaseModel], bare: dict[str, object]
) -> None:
    legacy = model.model_validate(bare)
    legacy_json = model.model_validate_json(json.dumps(bare))
    canonical = model.model_validate({"reply": bare})

    assert legacy == canonical
    assert legacy_json == canonical
    assert legacy.model_dump(mode="json") == canonical.model_dump(mode="json")
    assert set(legacy.model_dump(mode="json")) == {"reply"}


@pytest.mark.parametrize(
    ("_role", "model", "_bare"),
    _AFFECTED_ROLES,
    ids=[role for role, _, _ in _AFFECTED_ROLES],
)
def test_pre_envelope_wait_replies_remain_compatible(
    _role: str, model: type[BaseModel], _bare: dict[str, object]
) -> None:
    wait = {"kind": "waiting_for_evaluation", "handles": ["measurement"]}

    legacy = model.model_validate(wait)
    canonical = model.model_validate({"reply": wait})

    assert legacy == canonical
    assert legacy.model_dump(mode="json") == canonical.model_dump(mode="json")


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (_DYNAMIC["implementer"], []),
        (_DYNAMIC["judge"], []),
        (
            _SINGLE["implementer"],
            [
                "#/$defs/SingleAgentRoundResponse/properties/candidate_metrics allows arbitrary object keys"
            ],
        ),
        (
            _MULTI["implementer"],
            [
                "#/$defs/ImplementerResponse/properties/metrics allows arbitrary object keys",
                "#/$defs/ImplementerResponse/properties/candidate_metrics allows arbitrary object keys",
            ],
        ),
        (_MULTI["judge"], []),
        (_EVOLVE["mutator"], []),
        (_EVOLVE["judge"], []),
        (_ISSUE_QUEUE["implementer"], []),
        (_ISSUE_QUEUE["judge"], []),
    ],
)
def test_waitable_schemas_have_no_root_union_or_unresolved_reference(
    model: type[BaseModel], expected: list[str]
) -> None:
    assert native_schema_problems(model, "codex") == expected


@pytest.mark.parametrize("role", dynamic.roles, ids=lambda role: role.name)
def test_a_dynamic_role_answers_with_a_waitable_reply_exactly_when_it_may_wait(role: Role) -> None:
    """Only a role whose agent can request evaluations accepts the wait reply."""
    model = _DYNAMIC[role.name]
    wait = {"reply": {"kind": "waiting_for_evaluation", "handles": ["measurement"]}}

    assert issubclass(model, WaitableReply) is role.may_wait_for_evaluations
    assert native_schema_problems(model, "codex") == []
    if not role.may_wait_for_evaluations:
        with pytest.raises(ValidationError):
            model.model_validate(wait)


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (_DYNAMIC["implementer"], {"reply": _IMPLEMENTER}),
        (_DYNAMIC["judge"], {"reply": _JUDGE}),
        (
            _ISSUE_QUEUE["implementer"],
            {"reply": _ISSUE_QUEUE_IMPLEMENTER},
        ),
        (_ISSUE_QUEUE["judge"], {"reply": _ISSUE_QUEUE_JUDGE}),
    ],
)
def test_waitable_replies_reach_codex_as_native_schemas(
    tmp_path: Path, model: type[BaseModel], payload: dict[str, object]
) -> None:
    executor = FakeExecutor(scripted_turn("codex", structured_output=payload))
    launcher = fake_agentshim_launcher(provider="codex", executor=executor, threads=OsThreads())

    with AgentClient(
        launcher, provider="codex", clock=ManualClock(), threads=OsThreads()
    ) as client:
        result = client.invoke(
            kind="implementer",
            workspace=tmp_path,
            system_prompt="test",
            user_prompt="reply",
            response_cls=model,
            round_label="waitable reply schema",
        )

    assert result.model_dump(mode="json")["reply"] is not None
    assert "--output-schema" in executor.requests[-1].argv


@pytest.mark.parametrize(
    ("prompt", "wait_json"),
    [
        (
            render_template("core_protocol/agent_evaluation.j2", agent_evaluation=True),
            '{"reply":{"kind":"waiting_for_evaluation","handles":["<handle>"]}}',
        ),
        (
            TemplateRenderer(Path(issue_queue_prompts.__file__).parent).render_template(
                "agent_evaluation.j2", agent_evaluation=True
            ),
            '{"reply":{"kind":"waiting_for_evaluation","handles":["<handle>"]}}',
        ),
    ],
    ids=(
        "shared-role-fragment",
        "issue-queue-role-fragment",
    ),
)
@pytest.mark.parametrize(
    ("_role", "model", "_bare"),
    _AFFECTED_ROLES,
    ids=[role for role, _, _ in _AFFECTED_ROLES],
)
def test_rendered_wait_examples_are_accepted_by_every_waitable_role_schema(
    prompt: str,
    wait_json: str,
    _role: str,
    model: type[BaseModel],
    _bare: dict[str, object],
) -> None:
    assert f"`{wait_json}`" in prompt
    model.model_validate_json(wait_json)
