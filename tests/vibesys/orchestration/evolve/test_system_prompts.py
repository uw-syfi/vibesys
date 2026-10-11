"""Agent roles' system prompts are empty by design: the task text lives in the per-turn template."""

from __future__ import annotations

from vibesys.evolve_roles import CORE_ROLES


def test_system_prompts_are_empty() -> None:
    assert [role.system_prompt for role in CORE_ROLES] == ["", "", ""]
