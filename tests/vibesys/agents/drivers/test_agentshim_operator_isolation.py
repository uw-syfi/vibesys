"""A session's behavior does not depend on the operator who launches the run.

Two inputs used to leak from the operator into every agent session: their own
CLI configuration (settings, hooks, global instructions, memory in the
provider's state root) and their shell environment (credentials for unrelated
services, the parent agent's session variables, sockets). The driver now asks
agentshim for ``ConfigScope.PROJECT`` wherever the provider supports it, gives
a provider that can only isolate in a dedicated state root a run-owned home,
and passes a session only an allowlisted part of the launcher environment.

Every turn is scripted with ``agentshim.testing``; host confinement is switched
off through the launcher environment's own ``VIBESYS_AGENT_SANDBOX`` control,
so these tests observe the exact environment the provider process receives.
"""

from __future__ import annotations

import string
from typing import TYPE_CHECKING

import agentshim
import pytest
from agentshim.testing import FakeExecutor, scripted_turn
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vs_agent.api import session_env_allowlist
from vs_agent.contracts import AgentExecutionPolicy, AgentSessionSpec, AgentTurnRequest
from vs_agent.drivers.agentshim import AgentShimDriver
from vs_sandbox.api import SANDBOX_DISABLE_ENV

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

PROVIDERS = ("claude", "codex", "gemini", "opencode")
ROLES = ("planner", "implementer", "judge", "profiler")

#: What a launcher environment carries on a real host, none of it the agent's.
OPERATOR_ONLY = {
    "GOOGLE_APPLICATION_CREDENTIALS": "/home/op/gcp.json",
    "CLAUDE_EFFORT": "max",
    "CLAUDE_CODE_SESSION_ID": "parent-session",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "HERDR_SOCKET": "/run/herdr.sock",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
    "SSH_AUTH_SOCK": "/run/user/1000/ssh-agent.sock",
    "VIRTUAL_ENV": "/home/op/venv",
    "UV_CACHE_DIR": "/home/op/.cache/uv",
}


def _operator_home(root: Path) -> Path:
    """An operator HOME whose provider state holds their own configuration."""
    home = root / "operator"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / "auth.json").write_text('{"tokens": {"refresh_token": "operator"}}')
    (codex / "AGENTS.md").write_text("OPERATOR INSTRUCTION")
    (codex / "hooks.json").write_text("{}")
    (home / ".claude").mkdir()
    (home / ".claude" / "CLAUDE.md").write_text("OPERATOR INSTRUCTION")
    return home


def _launcher(home: Path, extra: Mapping[str, str] = OPERATOR_ONLY) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "xterm",
        SANDBOX_DISABLE_ENV: "off",
        **extra,
    }


def _spec(root: Path, provider: str, role: str = "implementer") -> AgentSessionSpec:
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    return AgentSessionSpec(
        role=role,
        provider=provider,
        workspace=workspace,
        policy=AgentExecutionPolicy(require_enforcement=False),
        environment=(("GPU", "0"),),
    )


def _turn(
    spec: AgentSessionSpec,
    *,
    agent_homes: Path | None,
    launcher: Mapping[str, str],
    passthrough: tuple[str, ...] = (),
) -> tuple[AgentShimDriver, agentshim.CommandRequest, list[str]]:
    provider = spec.provider
    fake = FakeExecutor(scripted_turn(provider, text="ok"))
    logs: list[str] = []
    driver = AgentShimDriver(
        provider=provider,
        executor_factory=lambda: fake,
        agent_homes=agent_homes,
        env_passthrough=passthrough,
        launcher_env=lambda: launcher,
        log=logs.append,
    )
    session = driver.create_session(spec)
    session.run_turn(AgentTurnRequest(message="go"))
    return driver, fake.requests[-1], logs


def _scope_args(profile: agentshim.ProviderProfile, env: Mapping[str, str]) -> list[str]:
    """The arguments agentshim adds for ``ConfigScope.PROJECT`` over ``ALL``."""
    provider = agentshim.get_provider(profile.name)

    def argv(scope: agentshim.ConfigScope) -> list[str]:
        return provider.build_argv(
            agentshim.ArgvContext(
                binary_path="cli",
                model=None,
                env=env,
                resume_session_id=None,
                reasoning_effort=None,
                schema_inline=None,
                schema_path=None,
                config_scope=scope,
            )
        )

    project = argv(agentshim.ConfigScope.PROJECT)
    default = argv(agentshim.ConfigScope.ALL)
    return [arg for arg in project if arg not in default]


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("role", ROLES)
def test_every_role_loads_no_operator_configuration_where_the_provider_can_enforce_it(
    tmp_path: Path, provider: str, role: str
) -> None:
    profile = agentshim.get_provider(provider).profile
    isolates = agentshim.ConfigScope.PROJECT in profile.config_scopes
    launcher = _launcher(_operator_home(tmp_path))

    driver, request, logs = _turn(
        _spec(tmp_path, provider, role), agent_homes=tmp_path / "agent-homes", launcher=launcher
    )

    assert driver.capabilities.config_isolation is isolates
    assert any("operator's own CLI configuration" in line for line in logs) is not isolates
    if isolates:
        added = _scope_args(profile, request.env)
        assert added
        assert all(arg in request.argv for arg in added)
    if profile.config_home_files:
        home = tmp_path / "agent-homes" / provider
        assert request.env[profile.state_root_env or ""] == str(home)
        # The run home holds the login and the CLI's own state, never the
        # operator's instructions or hooks.
        assert not (home / "AGENTS.md").exists()
        assert not (home / "hooks.json").exists()


def test_a_codex_home_shares_the_operators_login_and_survives_across_sessions(
    tmp_path: Path,
) -> None:
    operator = _operator_home(tmp_path)
    homes = tmp_path / "agent-homes"
    _turn(_spec(tmp_path, "codex"), agent_homes=homes, launcher=_launcher(operator))
    (homes / "codex" / "sessions" / "rollout.jsonl").write_text("{}")

    _turn(_spec(tmp_path, "codex"), agent_homes=homes, launcher=_launcher(operator))

    login = homes / "codex" / "auth.json"
    assert login.resolve() == (operator / ".codex" / "auth.json").resolve()
    assert (homes / "codex" / "sessions" / "rollout.jsonl").is_file()


def test_codex_without_a_run_home_keeps_the_operator_configuration_and_says_so(
    tmp_path: Path,
) -> None:
    launcher = _launcher(_operator_home(tmp_path))
    driver, request, logs = _turn(_spec(tmp_path, "codex"), agent_homes=None, launcher=launcher)

    assert driver.capabilities.config_isolation is False
    assert "CODEX_HOME" not in request.env
    assert any("operator's own CLI configuration" in line for line in logs)


_NAME = st.text(string.ascii_uppercase + string.digits + "_", min_size=1, max_size=12).filter(
    lambda name: not name[0].isdigit()
)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=40)
@given(
    provider=st.sampled_from(PROVIDERS),
    extra=st.dictionaries(
        st.one_of(st.sampled_from(sorted(OPERATOR_ONLY)), _NAME), st.text(max_size=8)
    ),
    passthrough=st.lists(_NAME, max_size=3, unique=True),
)
def test_no_variable_outside_the_allowlist_or_the_run_reaches_a_session(
    tmp_path_factory: pytest.TempPathFactory,
    provider: str,
    extra: dict[str, str],
    passthrough: list[str],
) -> None:
    root = tmp_path_factory.mktemp("case")
    launcher = _launcher(_operator_home(root), {**OPERATOR_ONLY, **extra})
    profile = agentshim.get_provider(provider).profile

    _driver, request, _logs = _turn(
        _spec(root, provider),
        agent_homes=root / "agent-homes",
        launcher=launcher,
        passthrough=tuple(passthrough),
    )

    allowed = session_env_allowlist(profile, passthrough) | {"GPU"}
    leaked = {name for name in request.env if name not in allowed and not name.startswith("LC_")}
    assert leaked == set()
    # A passthrough name the launcher sets does reach the session.
    for name in passthrough:
        if name in launcher:
            assert request.env[name] == launcher[name] or name == profile.state_root_env


def test_an_env_passthrough_entry_that_is_not_a_variable_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="'BAD-NAME'"):
        AgentShimDriver(provider="claude", env_passthrough=("BAD-NAME",))
