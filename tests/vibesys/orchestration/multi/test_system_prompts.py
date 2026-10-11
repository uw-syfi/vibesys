"""The fixed system prompts of the multi roles are pinned verbatim."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vibesys.multi_roles import DESIGNER, IMPLEMENTER, JUDGE, PROFILER

if TYPE_CHECKING:
    from vs_runtime.api import AgentRole

_FIXTURES = Path(__file__).parent / "fixtures" / "exact_prompts"


@pytest.mark.parametrize(
    ("role", "name"),
    [
        (DESIGNER, "system-orchestrator.txt"),
        (PROFILER, "system-profiler.txt"),
        (IMPLEMENTER, "system-implementer.txt"),
        (JUDGE, "system-judge.txt"),
    ],
    ids=("designer", "profiler", "implementer", "judge"),
)
def test_a_role_declares_its_pinned_system_prompt(role: AgentRole, name: str) -> None:
    assert str(role.system_prompt) == (_FIXTURES / name).read_text()
