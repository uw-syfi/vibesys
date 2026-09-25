"""Regression tests for issue_queue's turn-to-client session-reuse policy.

Before phase 3c, ``issue_queue/loop.py`` called ``turn_structured`` without
``reuse_session`` for the implementer, judge, and perf-eval roles, so the
real client (``vs_agent.client._invoke_turn``) applied its own default:
``reuse_session=None`` -> ``reuse=True`` with a per-role fallback session key
(``AgentSessionKey(SessionScope.ROLE, kind)``), never a caller-scoped key.

Phase 3c gave these roles ``session=Fresh()``, which forces
``reuse_session=False`` on every turn -- a new agent session per turn,
never reusing conversation history across issues. ``Reuse()`` restores the
"let the client pick its default" policy: ``ctx.agents.turn`` must pass
``reuse_session=None`` and no ``session_key``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from tests.vibesys.orchestration.harness import run_with_context

from vibesys import roles
from vibesys.roles.issue_queue import ISSUE_IMPLEMENTER, ISSUE_JUDGE, ISSUE_PERF_EVAL
from vibesys.runtime import Reuse, Role
from vs_agent.api import AgentSessionKey, SessionScope
from vs_agent.api.testing import FakeAgentClient

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.orchestration.runtime import RunContext

_ROLES = (ISSUE_IMPLEMENTER, ISSUE_JUDGE, ISSUE_PERF_EVAL)


def test_all_issue_queue_roles_declare_reuse_session_policy() -> None:
    for role in _ROLES:
        assert isinstance(role.session, Reuse), (
            f"{role.id}: issue_queue roles must use Reuse() to match pre-3c "
            "turn_structured(reuse_session=<unset>) semantics"
        )


def _write_fixture_template(root: Path, role_id: str) -> None:
    folder = root / "loops" / "issue_queue" / _leaf(role_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "system.j2").write_text("issue_queue prompt.\n")
    (root / "shared").mkdir(parents=True, exist_ok=True)


def _leaf(role_id: str) -> str:
    return {"implementer": "implementer", "judge": "judge", "perf_eval": "perf_eval"}[role_id]


def test_reuse_policy_sends_no_reuse_session_bool_and_no_session_key(tmp_path: Path) -> None:
    """``ctx.agents.turn`` must pass ``reuse_session=None`` and no caller-scoped

    ``session_key``, letting the client fall back to its own per-role default
    (``AgentSessionKey(SessionScope.ROLE, kind)``) exactly as the pre-3c
    ``turn_structured(...)`` calls (with neither argument) did.
    """
    for role in _ROLES:
        runner = FakeAgentClient(backend_name="stub")
        runner.enqueue(role.id, role.fallback())

        async def body(ctx: RunContext, role: Role = role) -> None:
            agent = await ctx.agents.spawn(ctx.agents.default_definition(role.id))
            try:
                await ctx.agents.turn(
                    role,
                    agent=agent,
                    context={},
                    label="reuse-check",
                )
            finally:
                await agent.close()

        prompts_root = tmp_path / f"prompts_fixture_{role.id}"
        _write_fixture_template(prompts_root, role.id)
        with patch("vibesys.orchestration.agents.PROMPTS_DIR", prompts_root):
            run_with_context(tmp_path, runner, body)

        call = runner.calls_for(role.id)[0]
        assert call.reuse_session is None, (
            f"{role.id}: expected reuse_session=None, got {call.reuse_session!r}"
        )
        # The driver layer resolves an unscoped reuse to the same per-role
        # fallback key the real client uses (vs_agent.client._invoke_turn):
        # never a caller-supplied, per-issue key.
        assert call.session_key == AgentSessionKey(SessionScope.ROLE, role.id), (
            f"{role.id}: expected the per-role fallback session key, got {call.session_key!r}"
        )


def test_issue_queue_roles_are_registered_in_the_catalog() -> None:
    for role in _ROLES:
        assert role in roles.ALL_ROLES
