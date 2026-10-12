"""Waitable role replies stay native on Codex and accept pre-envelope results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vibesys.orchestration.dynamic.core_policy.api import reply_schemas as dynamic_schemas
from vibesys.orchestration.dynamic.strategy.api import (
    IMPLEMENTER_REPLY as DYNAMIC_IMPLEMENTER_REPLY,
)
from vibesys.orchestration.dynamic.strategy.api import JUDGE_REPLY as DYNAMIC_JUDGE_REPLY
from vibesys.orchestration.dynamic.strategy.api import (
    PROFILER_REPLY as DYNAMIC_PROFILER_REPLY,
)
from vibesys.orchestration.dynamic.strategy.api import DynamicConfig
from vibesys.orchestration.evolve.core_policy.api import reply_schemas as evolve_schemas
from vibesys.orchestration.evolve.strategy.api import JUDGE_REPLY as EVOLVE_JUDGE_REPLY
from vibesys.orchestration.evolve.strategy.api import MUTATOR_REPLY as EVOLVE_MUTATOR_REPLY
from vibesys.orchestration.issue_queue.core_policy import prompts as issue_queue_prompts
from vibesys.orchestration.issue_queue.core_policy.api import (
    reply_schemas as issue_queue_schemas,
)
from vibesys.orchestration.issue_queue.strategy.api import (
    IMPLEMENTER_REPLY as ISSUE_QUEUE_IMPLEMENTER_REPLY,
)
from vibesys.orchestration.issue_queue.strategy.api import (
    JUDGE_REPLY as ISSUE_QUEUE_JUDGE_REPLY,
)
from vibesys.orchestration.issue_queue.strategy.api import IssueQueueConfig
from vibesys.orchestration.multi.core_policy.api import reply_schemas as multi_schemas
from vibesys.orchestration.multi.strategy.api import IMPLEMENTER_REPLY as MULTI_IMPLEMENTER_REPLY
from vibesys.orchestration.multi.strategy.api import JUDGE_REPLY as MULTI_JUDGE_REPLY
from vibesys.orchestration.single.core_policy.api import reply_schemas as single_schemas
from vibesys.orchestration.single.strategy.api import IMPLEMENTER_REPLY as SINGLE_IMPLEMENTER_REPLY
from vibesys.prompts import render_template
from vs_agent.api import AgentClient, native_schema_problems
from vs_agent.api.testing import FakeExecutor, fake_agentshim_launcher, scripted_turn
from vs_core.api import ArtifactId, ArtifactRef
from vs_prompts.api import TemplateRenderer

if TYPE_CHECKING:
    from pydantic import BaseModel


_DYNAMIC = dynamic_schemas(
    DynamicConfig(
        recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe"),
        max_rounds=1,
    )
)
_EVOLVE = evolve_schemas()
_SINGLE = single_schemas()
_MULTI = multi_schemas()
_ISSUE_QUEUE = issue_queue_schemas(
    IssueQueueConfig(
        recipe=ArtifactRef(artifact_id=ArtifactId(root="issue-recipe"), digest="issue-recipe"),
        max_rounds=1,
        max_attempts_per_issue=1,
        max_issues_per_perf_eval=1,
        max_issues_per_round=1,
    )
)

_IMPLEMENTER = {"summary": "done", "outcome": "supported"}
_JUDGE = {"passed": True, "analysis": "checked"}
_SINGLE_RESULT = {
    "summary": "done",
    "expected_behavior": "faster",
    "self_review": "checked",
    "feedback": "",
    "verdict": "pass",
    "bottlenecks": "",
    "suggestions": "",
    "profile_analysis": "",
}
_MULTI_IMPLEMENTER = {"summary": "done", "expected_behavior": "faster"}
_EVOLVE_MUTATOR = {"summary": "did it", "hypothesis": "because", "expected_behavior": "faster"}
_EVOLVE_JUDGE = {"analysis": "checked", "feedback": "", "verdict": "pass"}
_MULTI_JUDGE = {"analysis": "checked", "feedback": "", "verdict": "pass"}
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
    ("dynamic-implementer", _DYNAMIC[DYNAMIC_IMPLEMENTER_REPLY], _IMPLEMENTER),
    ("dynamic-profiler", _DYNAMIC[DYNAMIC_PROFILER_REPLY], _IMPLEMENTER),
    ("dynamic-judge", _DYNAMIC[DYNAMIC_JUDGE_REPLY], _JUDGE),
    ("evolve-mutator", _EVOLVE[EVOLVE_MUTATOR_REPLY], _EVOLVE_MUTATOR),
    ("evolve-judge", _EVOLVE[EVOLVE_JUDGE_REPLY], _EVOLVE_JUDGE),
    ("single-implementer", _SINGLE[SINGLE_IMPLEMENTER_REPLY], _SINGLE_RESULT),
    ("multi-implementer", _MULTI[MULTI_IMPLEMENTER_REPLY], _MULTI_IMPLEMENTER),
    ("multi-judge", _MULTI[MULTI_JUDGE_REPLY], _MULTI_JUDGE),
    (
        "issue-queue-implementer",
        _ISSUE_QUEUE[ISSUE_QUEUE_IMPLEMENTER_REPLY],
        _ISSUE_QUEUE_IMPLEMENTER,
    ),
    ("issue-queue-judge", _ISSUE_QUEUE[ISSUE_QUEUE_JUDGE_REPLY], _ISSUE_QUEUE_JUDGE),
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
        (_DYNAMIC[DYNAMIC_IMPLEMENTER_REPLY], []),
        (_DYNAMIC[DYNAMIC_PROFILER_REPLY], []),
        (_DYNAMIC[DYNAMIC_JUDGE_REPLY], []),
        (
            _SINGLE[SINGLE_IMPLEMENTER_REPLY],
            [
                "#/$defs/SingleAgentRoundResponse/properties/candidate_metrics allows arbitrary object keys"
            ],
        ),
        (
            _MULTI[MULTI_IMPLEMENTER_REPLY],
            [
                "#/$defs/ImplementerResponse/properties/metrics allows arbitrary object keys",
                "#/$defs/ImplementerResponse/properties/candidate_metrics allows arbitrary object keys",
            ],
        ),
        (_MULTI[MULTI_JUDGE_REPLY], []),
        (_EVOLVE[EVOLVE_MUTATOR_REPLY], []),
        (_EVOLVE[EVOLVE_JUDGE_REPLY], []),
        (_ISSUE_QUEUE[ISSUE_QUEUE_IMPLEMENTER_REPLY], []),
        (_ISSUE_QUEUE[ISSUE_QUEUE_JUDGE_REPLY], []),
    ],
)
def test_waitable_schemas_have_no_root_union_or_unresolved_reference(
    model: type[BaseModel], expected: list[str]
) -> None:
    assert native_schema_problems(model, "codex") == expected


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (_DYNAMIC[DYNAMIC_IMPLEMENTER_REPLY], {"reply": _IMPLEMENTER}),
        (_DYNAMIC[DYNAMIC_PROFILER_REPLY], {"reply": _IMPLEMENTER}),
        (_DYNAMIC[DYNAMIC_JUDGE_REPLY], {"reply": _JUDGE}),
        (
            _ISSUE_QUEUE[ISSUE_QUEUE_IMPLEMENTER_REPLY],
            {"reply": _ISSUE_QUEUE_IMPLEMENTER},
        ),
        (_ISSUE_QUEUE[ISSUE_QUEUE_JUDGE_REPLY], {"reply": _ISSUE_QUEUE_JUDGE}),
    ],
)
def test_waitable_replies_reach_codex_as_native_schemas(
    tmp_path: Path, model: type[BaseModel], payload: dict[str, object]
) -> None:
    executor = FakeExecutor(scripted_turn("codex", structured_output=payload))
    launcher = fake_agentshim_launcher(provider="codex", executor=executor)

    with AgentClient(launcher, provider="codex") as client:
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
