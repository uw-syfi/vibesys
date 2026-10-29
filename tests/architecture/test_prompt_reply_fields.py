"""Prompt templates never type the name of a reply field.

The agent's reply schema is the one source of truth for reply field names. A template that
renders text for a turn with a reply model writes ``{{ reply.some_field }}``: the render
owner derives ``reply`` from that turn's reply model as the run offers it (see
``vs_prompts.api.ReplyFields``), so a renamed, removed, or capability-trimmed field fails
the render instead of leaving a prompt that describes a reply the agent cannot give.

This check bans, in every ``.j2`` file under ``src/vibesys`` and ``libs``, an inline code
span that opens with the name of a field of any registered reply model (a backticked
``name``, ``name=false`` or ``name: null``). The name must end at a non-identifier
character, so a longer word never matches, and a call such as ``summary(path=...)`` is a
tool, not a field. Jinja comments are not agent-bound text and are skipped.

A span that merely shares a field's name with something else is listed in
``_NOT_A_REPLY_FIELD`` with the number of occurrences allowed in that file and the reason.
The count is exact: a new occurrence fails, and so does a removed one until the entry is
updated or deleted, so the list only shrinks.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest
from tests.support.prompt_context import registered_reply_models

from vs_prompts.api import reply_fields

_REPO_ROOT = Path(__file__).resolve().parents[2]
# `src/server` renders chat prompts, which have no reply model.
_TEMPLATE_ROOTS = (_REPO_ROOT / "src" / "vibesys", _REPO_ROOT / "libs")

_TOOL_RESULT = "the evaluation tool's own result field, not a field of an agent reply"
_ISSUE_QUEUE = "src/vibesys/orchestration/issue_queue/core_policy/prompts"
_PROFILERS = "src/vibesys/prompts/shared/profilers"
_NOT_A_REPLY_FIELD: dict[tuple[str, str], tuple[int, str]] = {
    (f"{_ISSUE_QUEUE}/judge_system_prompt.j2", "new_issues"): (
        1,
        "a system prompt is rendered once per role before any run capability exists, so it "
        "has no `reply`; the judge's schema always offers the field",
    ),
    ("src/vibesys/prompts/shared/_notices/regression.j2", "base_round"): (
        2,
        "no strategy includes this notice; only the notice goldens render it, without a reply",
    ),
    (f"{_ISSUE_QUEUE}/agent_evaluation.j2", "evidence"): (1, _TOOL_RESULT),
    (f"{_ISSUE_QUEUE}/agent_evaluation.j2", "outcome"): (1, _TOOL_RESULT),
    ("src/vibesys/prompts/core_protocol/agent_evaluation.j2", "evidence"): (1, _TOOL_RESULT),
    ("src/vibesys/prompts/core_protocol/agent_evaluation.j2", "outcome"): (1, _TOOL_RESULT),
    ("src/vibesys/prompts/backend/trainium/profiling_workflow.j2", "summary"): (
        1,
        "a neuron-explorer subcommand",
    ),
    ("src/vibesys/prompts/environments/modal/runtime_notes.j2", "summary"): (
        1,
        "a keyword argument of an example Modal function",
    ),
    (f"{_PROFILERS}/neuron.j2", "summary"): (1, "a neuron-explorer subcommand"),
    (f"{_PROFILERS}/rocprof.j2", "summary"): (
        3,
        "a name in the profiler tool server's capture tool table",
    ),
    (f"{_PROFILERS}/torch.j2", "summary"): (1, "a profiler tool named in its tool list"),
}

_FIELDS = frozenset(
    name for model in registered_reply_models() for name in reply_fields(model.model_json_schema())
)
_SPAN = re.compile(r"(?<![\w{])`([a-z][a-z0-9_]*)(?![a-z0-9_(])")
_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)


def typed_names(source: str, fields: frozenset[str]) -> list[tuple[int, str]]:
    """The (line, name) of every typed reply field name in template ``source``."""
    text = _COMMENT.sub(lambda comment: "\n" * comment.group().count("\n"), source)
    return [
        (number, match.group(1))
        for number, line in enumerate(text.splitlines(), 1)
        for match in _SPAN.finditer(line)
        if match.group(1) in fields
    ]


def _templates() -> list[Path]:
    return sorted(path for root in _TEMPLATE_ROOTS for path in root.rglob("*.j2"))


def _relative(template: Path) -> str:
    return template.relative_to(_REPO_ROOT).as_posix()


def _counts(template: Path) -> Counter[str]:
    return Counter(name for _, name in typed_names(template.read_text("utf-8"), _FIELDS))


def test_the_check_knows_the_reply_fields_the_prompts_describe() -> None:
    assert {"hypothesis_id", "parent_hypothesis_id", "perf_metric", "next_step"} <= _FIELDS


@pytest.mark.parametrize(
    "source",
    [
        "set `task` and `pass_criteria`",
        "leave `parent_hypothesis_id` null.",
        "set `need_profile=false` now",
        "report `perf_metric: null`",
        "a\nb `task`",
    ],
)
def test_the_scanner_flags_a_typed_field_name(source: str) -> None:
    assert typed_names(
        source,
        frozenset({"task", "pass_criteria", "parent_hypothesis_id", "need_profile", "perf_metric"}),
    )


@pytest.mark.parametrize(
    "source",
    [
        "set {{ reply.task }} and {{ reply.pass_criteria }}",
        "set `{{ reply.need_profile.name }}=false`",
        "{# `task` is only a comment #}",
        "call `task(path=1)` or `tasks` or `my_task`",
        "a `{{ task }}` variable",
    ],
)
def test_the_scanner_passes_a_reference_through_reply(source: str) -> None:
    assert not typed_names(source, frozenset({"task", "pass_criteria", "need_profile"}))


def test_the_scanner_reports_the_line_of_a_name() -> None:
    assert typed_names("a\n{# x\n#}\nb `task`", frozenset({"task"})) == [(4, "task")]


@pytest.mark.parametrize("template", _templates(), ids=_relative)
def test_a_template_names_reply_fields_only_through_reply(template: Path) -> None:
    allowed = {
        name: count
        for (path, name), (count, _) in _NOT_A_REPLY_FIELD.items()
        if path == _relative(template)
    }
    excess = {
        name: count for name, count in _counts(template).items() if count != allowed.get(name, 0)
    }

    assert not excess, (
        f"{_relative(template)} types reply field names {excess} (allowed: {allowed}). "
        "Write {{ reply.<field> }} so the reply schema stays the source of truth."
    )


def test_every_exemption_still_matches_exactly() -> None:
    stale = {
        entry: (count, _counts(_REPO_ROOT / entry[0])[entry[1]])
        for entry, (count, _) in _NOT_A_REPLY_FIELD.items()
        if _counts(_REPO_ROOT / entry[0])[entry[1]] != count
    }

    assert not stale, f"exemptions whose (allowed, actual) counts differ: {stale}"
