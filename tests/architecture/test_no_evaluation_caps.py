"""Plans and prompts carry no cap on evaluations (#3047).

Evaluations have no allowance: an agent measures whenever measuring tells it something, bounded
only by its turn's time limit, and a conservative runaway flag warns of an egregious count
without refusing it (`vs_core._runaway`). A plan that stated "at most two paid target calls" made
agents run out of measurements and end ideas inconclusive, so no plan field and no prompt text
may state or ask for such a cap.

Two checks keep it so: the typed plans have no field for one, and no template tells a planner to
state a cap or an implementer or judge to tally and enforce one.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vibesys.hypothesis import OrchestratorPlan
from vibesys.orchestration.dynamic.models import (
    ImplementPortfolioPlan,
    PortfolioPlan,
    WorkstreamPlan,
)

if TYPE_CHECKING:
    from pydantic import BaseModel

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_ROOTS = (_REPO_ROOT / "src", _REPO_ROOT / "libs")

_CAP_FIELD = re.compile(
    r"cap|allowance|budget|max_(?:eval|call|paid|target|measure)", re.IGNORECASE
)
_CAP_TEXT = re.compile(
    r"hard (?:paid-call )?maxim|paid[- ]cap|paid-work bounds|cumulative (?:paid|hypothesis)|"
    r"paid-call maxima|cap change|consumed against|replenish",
    re.IGNORECASE,
)


@pytest.mark.parametrize(
    "plan", [OrchestratorPlan, PortfolioPlan, ImplementPortfolioPlan, WorkstreamPlan]
)
def test_a_typed_plan_has_no_field_for_a_cap_on_evaluations(plan: type[BaseModel]) -> None:
    assert [name for name in plan.model_fields if _CAP_FIELD.search(name)] == []


def _templates() -> list[Path]:
    return sorted(
        path
        for root in _TEMPLATE_ROOTS
        for path in root.rglob("*.j2")
        if "node_modules" not in path.parts
    )


@pytest.mark.parametrize(
    "template", _templates(), ids=lambda path: str(path.relative_to(_REPO_ROOT))
)
def test_no_template_states_or_enforces_a_cap_on_evaluations(template: Path) -> None:
    found = [match.group() for match in _CAP_TEXT.finditer(template.read_text(encoding="utf-8"))]
    assert found == [], f"{template.name} states or enforces an evaluation cap: {found}"
