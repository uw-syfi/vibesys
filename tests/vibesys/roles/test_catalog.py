"""Sanity checks for the ``vibesys.roles`` catalog.

Does not (yet) assert every declared role is referenced by a strategy:
none of ``loops/`` calls ``ctx.agents.turn`` in phase 3a (see the phase-3a
report), so that "unused role" invariant only becomes meaningful once
loops/ migrates in a later phase.
"""

from __future__ import annotations

from pathlib import Path

from vibesys import roles
from vibesys.prompts import PROMPTS_DIR
from vibesys.runtime import Role


def test_every_role_has_a_real_template_file() -> None:
    for role in roles.ALL_ROLES:
        parts = Path(role.template).parts
        assert len(parts) >= 3, f"{role.id}: template {role.template!r} needs a strategy folder"
        # Resolve exactly as `_Agents.turn` does: strategy folder as the
        # primary root, remainder as the name, falling back to shared/.
        template_dir = PROMPTS_DIR / parts[0] / parts[1]
        name = "/".join(parts[2:])
        candidates = (template_dir / name, PROMPTS_DIR / "shared" / name, PROMPTS_DIR / name)
        assert any(path.is_file() for path in candidates), (
            f"{role.id}: no template file found for {role.template!r} "
            f"(looked in {[str(c) for c in candidates]})"
        )


def test_role_ids_are_stable_agent_config_keys() -> None:
    # `role.id` is the agent-config lookup key (backend/model), same as
    # today's `default_definition(role_id)`. It must be a short, stable
    # token -- never derived from a strategy or template name.
    known = {"orchestrator", "implementer", "judge", "profiler", "perf_eval"}
    for role in roles.ALL_ROLES:
        assert role.id in known, f"unexpected role id {role.id!r}"


def test_no_two_roles_share_a_template_with_different_reply_types() -> None:
    seen: dict[str, type] = {}
    for role in roles.ALL_ROLES:
        assert isinstance(role, Role)
        prior = seen.get(role.template)
        if prior is not None:
            assert prior is role.reply, (
                f"template {role.template!r} declared with two different reply types"
            )
        seen[role.template] = role.reply
