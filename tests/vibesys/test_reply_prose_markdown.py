"""The replies agents hand off declare their free-text fields as Markdown, and only those.

The agent reads each field's description as its reply schema, so the convention is stated there,
built from one constant. Every plain-`str` field of a handoff reply model must either carry the
Markdown convention, or be named below as an identifier-like field (an id, a path, a unit, a
title) that stays plain text. A new free-text field that nobody classified fails here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vibesys.hypothesis import HypothesisStrategyUpdate, OrchestratorPlan
from vibesys.orchestration.multi.models import ImplementerResponse, JudgeResponse, PreRoundDecision
from vibesys.orchestration.profilers import ProfilerSummary
from vibesys.orchestration.single.models import SingleAgentRoundResponse
from vs_core.api import MARKDOWN_PROSE_CONVENTION, PLAIN_LINE_CONVENTION
from vs_issue_tracker.api import RoadmapItem

if TYPE_CHECKING:
    from pydantic import BaseModel

_MODELS: tuple[type[BaseModel], ...] = (
    OrchestratorPlan,
    HypothesisStrategyUpdate,
    RoadmapItem,
    ImplementerResponse,
    JudgeResponse,
    PreRoundDecision,
    SingleAgentRoundResponse,
    ProfilerSummary,
)

# Identifier-like fields (ids, units, paths, operating-point names) hold a value another part of
# the system reads, so they stay plain text and say nothing about Markdown.
_IDENTIFIERS = {
    "OrchestratorPlan": {"hypothesis_id"},
    "HypothesisStrategyUpdate": {"hypothesis_id"},
    "RoadmapItem": {"id", "major"},
    "ImplementerResponse": {
        "perf_unit",
        "evaluation_artifact",
        "candidate_evaluation_artifact",
        "candidate_operating_point",
    },
    "SingleAgentRoundResponse": {
        "perf_unit",
        "candidate_evaluation_artifact",
        "candidate_operating_point",
    },
    "ProfilerSummary": {"perf_unit"},
}
# Title-like fields are one plain line, and say so.
_PLAIN_LINES = {"OrchestratorPlan": {"title"}, "RoadmapItem": {"title"}}


def _description(model: type[BaseModel], name: str) -> str:
    return str(model.model_json_schema()["properties"][name].get("description", ""))


def _plain_text_fields(model: type[BaseModel]) -> list[str]:
    return [
        name for name, info in model.model_fields.items() if info.annotation in (str, str | None)
    ]


def _cases() -> list[tuple[type[BaseModel], str]]:
    return [(model, name) for model in _MODELS for name in _plain_text_fields(model)]


@pytest.mark.parametrize(
    ("model", "name"), _cases(), ids=lambda value: getattr(value, "__name__", value)
)
def test_every_text_field_of_a_handoff_reply_is_classified_in_its_schema(
    model: type[BaseModel], name: str
) -> None:
    description = _description(model, name)
    identifier = name in _IDENTIFIERS.get(model.__name__, set())
    plain_line = name in _PLAIN_LINES.get(model.__name__, set())
    assert (MARKDOWN_PROSE_CONVENTION in description) is (not identifier and not plain_line)
    assert (PLAIN_LINE_CONVENTION in description) is plain_line
    if identifier:
        assert "Markdown" not in description


def test_the_markdown_convention_is_stated_once_per_description() -> None:
    for model, name in _cases():
        assert _description(model, name).count(MARKDOWN_PROSE_CONVENTION) <= 1


def test_the_classification_names_only_fields_that_exist() -> None:
    by_name = {model.__name__: set(_plain_text_fields(model)) for model in _MODELS}
    for classified in (_IDENTIFIERS, _PLAIN_LINES):
        for model_name, names in classified.items():
            assert names <= by_name[model_name]
