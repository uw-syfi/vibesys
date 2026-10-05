"""The roles a core dynamic run declares are exactly the roles its strategy names."""

from __future__ import annotations

import subprocess
import sys

from vibesys.dynamic_core import dynamic_core_registration
from vibesys.orchestration.dynamic.core_policy.api import CORE_ROLES
from vibesys.orchestration.dynamic.strategy.api import Role, role_id
from vs_runtime.api import AgentCapability

_LEGACY_ROLES = "vibesys.orchestration.dynamic.agents"


def test_every_strategy_role_is_declared_once_and_the_plugin_serves_them() -> None:
    declared = [role.id for role in CORE_ROLES]

    assert sorted(declared) == sorted(role_id(role.value).root for role in Role)
    assert tuple(dynamic_core_registration().plugin.agents) == CORE_ROLES


def test_a_core_role_answers_with_a_reply_and_needs_no_tool_or_mcp_server() -> None:
    for role in CORE_ROLES:
        assert role.extra_tools == ()
        assert AgentCapability.MCP_SERVERS not in role.required_capabilities
        assert str(role.system_prompt).strip()


def test_the_core_policy_does_not_load_the_legacy_role_package() -> None:
    """Deleting the legacy dynamic package must not break the core path."""
    probe = (
        f"import sys, vibesys.dynamic_core;sys.exit(1 if {_LEGACY_ROLES!r} in sys.modules else 0)"
    )
    # test-isolation: module loading is process-global, so only a fresh interpreter can tell.
    result = subprocess.run([sys.executable, "-c", probe], check=False, capture_output=True)  # noqa: S603
    assert result.returncode == 0, result.stderr.decode()
