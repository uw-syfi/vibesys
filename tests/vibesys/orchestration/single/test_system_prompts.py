"""The fixed system prompts of the single roles are pinned verbatim."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vibesys.single_roles import DESIGNER, IMPLEMENTER

if TYPE_CHECKING:
    from vs_runtime.api import AgentRole

_FIXTURES = Path(__file__).parent / "fixtures" / "exact_prompts"


@pytest.mark.parametrize(
    ("role", "name"),
    [(DESIGNER, "system-orchestrator.txt"), (IMPLEMENTER, "system-implementer.txt")],
    ids=("designer", "implementer"),
)
def test_a_role_declares_its_pinned_system_prompt(role: AgentRole, name: str) -> None:
    assert str(role.system_prompt) == (_FIXTURES / name).read_text()
