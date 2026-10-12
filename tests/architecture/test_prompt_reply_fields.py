"""Prompt templates never type the name of a reply field.

The agent's reply schema is the one source of truth for reply field names. A template that
renders text for a turn with a reply model writes ``{{ reply.some_field }}``: the render
owner derives ``reply`` from that turn's reply model as the run offers it (see
``vs_prompts.api.ReplyFields``), so a renamed, removed, or capability-trimmed field fails
the render instead of leaving a prompt that describes a reply the agent cannot give.

This check bans, in every ``.j2`` file under ``src/vibesys`` and ``libs``, an inline code
span that opens with the name of a field of any registered reply model (```name```,
```name=false```, ```name: null```). The name must end at a non-identifier character, so a
longer word never matches, and a call such as ```summary(path=...)``` is a tool, not a field.
Jinja comments are not agent-bound text and are skipped.

A span that merely shares a field's name with something else is listed in
``_NOT_A_REPLY_FIELD`` with the reason. The list only shrinks: an entry that no longer
matches fails until it is removed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from tests.vibesys.orchestration.dynamic.strategy._run import config as dynamic_config

from vibesys.orchestration.dynamic.core_policy.api import reply_schemas as dynamic_schemas
from vibesys.orchestration.evolve.core_policy.api import reply_schemas as evolve_schemas
from vibesys.orchestration.issue_queue.core_policy.api import reply_schemas as issue_queue_schemas
from vibesys.orchestration.issue_queue.strategy.api import IssueQueueConfig
from vibesys.orchestration.multi.core_policy.api import reply_schemas as multi_schemas
from vibesys.orchestration.single.core_policy.api import reply_schemas as single_schemas
from vs_core.api import ArtifactId, ArtifactRef
from vs_prompts.api import reply_fields
from vs_runtime.api import ProfileField

_REPO_ROOT = Path(__file__).resolve().parents[2]
# `src/server` renders chat prompts, which have no reply model.
_TEMPLATE_ROOTS = (_REPO_ROOT / "src" / "vibesys", _REPO_ROOT / "libs")

_RECIPE = ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe")

_TOOL_RESULT = "the evaluation tool's own result field, not a field of an agent reply"
_ISSUE_QUEUE = "src/vibesys/orchestration/issue_queue/core_policy/prompts"
_NOT_A_REPLY_FIELD: dict[tuple[str, str], str] = {
    (f"{_ISSUE_QUEUE}/judge_system_prompt.j2", "new_issues"): (
        "a system prompt is rendered once per role before any run capability exists, so it "
        "has no `reply`; the judge's schema always offers the field"
    ),
    (
        "src/vibesys/prompts/shared/_notices/regression.j2",
        "revert_to_round",
    ): "no strategy includes this notice; only the notice goldens render it, without a reply",
    (f"{_ISSUE_QUEUE}/agent_evaluation.j2", "evidence"): _TOOL_RESULT,
    (f"{_ISSUE_QUEUE}/agent_evaluation.j2", "outcome"): _TOOL_RESULT,
    ("src/vibesys/prompts/core_protocol/agent_evaluation.j2", "evidence"): _TOOL_RESULT,
    ("src/vibesys/prompts/core_protocol/agent_evaluation.j2", "outcome"): _TOOL_RESULT,
    (
        "src/vibesys/prompts/backend/trainium/profiling_workflow.j2",
        "summary",
    ): "a neuron-explorer subcommand",
    (
        "src/vibesys/prompts/environments/modal/runtime_notes.j2",
        "summary",
    ): "a keyword argument of an example Modal function",
    ("src/vibesys/prompts/shared/profilers/neuron.j2", "summary"): "a neuron-explorer subcommand",
    (
        "src/vibesys/prompts/shared/profilers/rocprof.j2",
        "summary",
    ): "a name in the profiler tool server's capture tool table",
    (
        "src/vibesys/prompts/shared/profilers/torch.j2",
        "summary",
    ): "a profiler tool named in its tool list",
}


def _registered_reply_field_names() -> frozenset[str]:
    """Every field offered by any reply model a strategy registers, under any capability."""
    models = [
        *dynamic_schemas(dynamic_config()).values(),
        *dynamic_schemas(
            dynamic_config(profiling=True, profile_fields=tuple(ProfileField))
        ).values(),
        *evolve_schemas().values(),
        *multi_schemas().values(),
        *single_schemas().values(),
    ]
    for domain in ("llm-serving", "generic"):
        models.extend(
            issue_queue_schemas(
                IssueQueueConfig(
                    recipe=_RECIPE,
                    domain_id=domain,
                    max_rounds=1,
                    max_attempts_per_issue=1,
                    max_issues_per_perf_eval=1,
                    max_issues_per_round=1,
                )
            ).values()
        )
    return frozenset(name for model in models for name in reply_fields(model.model_json_schema()))


_FIELDS = _registered_reply_field_names()
_SPAN = re.compile(r"(?<![\w{])`([a-z][a-z0-9_]*)(?![a-z0-9_(])")
_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)


def _templates() -> list[Path]:
    return sorted(path for root in _TEMPLATE_ROOTS for path in root.rglob("*.j2"))


def _typed_names(template: Path) -> set[tuple[int, str]]:
    source = template.read_text(encoding="utf-8")
    text = _COMMENT.sub(lambda comment: "\n" * comment.group().count("\n"), source)
    return {
        (number, match.group(1))
        for number, line in enumerate(text.splitlines(), 1)
        for match in _SPAN.finditer(line)
        if match.group(1) in _FIELDS
    }


def _relative(template: Path) -> str:
    return template.relative_to(_REPO_ROOT).as_posix()


def test_the_check_knows_the_reply_fields_the_prompts_describe() -> None:
    assert {"hypothesis_id", "parent_hypothesis_id", "perf_metric", "next_step"} <= _FIELDS


@pytest.mark.parametrize("template", _templates(), ids=_relative)
def test_a_template_names_reply_fields_only_through_reply(template: Path) -> None:
    typed = {
        (number, name)
        for number, name in _typed_names(template)
        if (_relative(template), name) not in _NOT_A_REPLY_FIELD
    }

    assert not typed, (
        f"{_relative(template)} types reply field names: "
        + ", ".join(f"line {number} `{name}`" for number, name in sorted(typed))
        + ". Write {{ reply.<field> }} so the reply schema stays the source of truth."
    )


def test_every_exemption_still_matches_a_typed_name() -> None:
    stale = [
        entry
        for entry in _NOT_A_REPLY_FIELD
        if entry[1] not in {name for _, name in _typed_names(_REPO_ROOT / entry[0])}
    ]

    assert not stale, f"remove exemptions that no longer match: {stale}"
