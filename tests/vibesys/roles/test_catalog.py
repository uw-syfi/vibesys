"""Sanity checks for the ``vibesys.roles`` catalog."""

from __future__ import annotations

import re
from pathlib import Path

from vibesys import roles
from vibesys.prompts import PROMPTS_DIR
from vibesys.runtime import Role

_LOOPS_SRC = Path(__file__).resolve().parents[3] / "src" / "vibesys" / "loops"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _role_search_names() -> dict[int, str]:
    """Map each ``Role``'s ``id()`` to the module-level name that finds it.

    A dict-of-``Role`` family (e.g. ``MULTI_PROFILERS``: one per profiler
    kind, no single named constant per ``Role``) maps every value to the
    dict's own name, since that's the identifier a strategy imports.
    """
    by_id: dict[int, str] = {}
    for family in (
        roles.designer,
        roles.pre_round,
        roles.implementer,
        roles.judge,
        roles.profiler,
        roles.single_agent,
        roles.perf_eval,
        roles.mutator,
    ):
        for name, value in vars(family).items():
            if name.startswith("_") or not _IDENTIFIER.match(name):
                continue
            if isinstance(value, Role):
                by_id[id(value)] = name
            elif (
                isinstance(value, dict)
                and value
                and all(isinstance(v, Role) for v in value.values())
            ):
                for v in value.values():
                    by_id[id(v)] = name
    return by_id


def test_every_role_in_the_catalog_is_used_by_a_registered_strategy() -> None:
    """Every ``Role`` any strategy uses lives in ``roles/``; the converse:
    every ``Role`` declared in ``roles/`` is reachable from a registered
    strategy's ``loops/`` folder, so the catalog carries no dead roles.
    """
    search_names = _role_search_names()
    assert {id(role) for role in roles.ALL_ROLES} <= search_names.keys(), (
        "a Role in roles.ALL_ROLES has no discoverable module-level name"
    )

    loops_source = "\n".join(
        path.read_text() for path in _LOOPS_SRC.rglob("*.py") if "roles" not in path.parts
    )
    unused = sorted(
        {
            search_names[id(role)]
            for role in roles.ALL_ROLES
            if not re.search(rf"\b{re.escape(search_names[id(role)])}\b", loops_source)
        }
    )
    assert not unused, f"roles declared but never referenced from loops/: {unused}"


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
