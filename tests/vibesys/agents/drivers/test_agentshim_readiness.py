"""A provider CLI that cannot start a turn is reported before its first turn.

The driver probes through agentshim, on the executor and environment a session
of the same spec runs with; the client turns a missing binary or a failed login
into :class:`ProviderNotReadyError` and lets an unknown login state proceed.
Everything is scripted with ``agentshim.testing``, so no CLI is needed.
"""

from __future__ import annotations

import io
from pathlib import Path

import agentshim
import pytest
from agentshim.testing import FakeExecutor, FakeRun, probe_executor, probe_run, scripted_turn
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api import (
    AgentClient,
    AuthStatus,
    ProviderNotReadyError,
    ReadinessProblem,
)
from vs_agent.client import AgentDiagnosticLog
from vs_agent.contracts import AgentExecutionPolicy, AgentSessionSpec, AgentTurnRequest
from vs_agent.drivers.agentshim import AgentShimDriver
from vs_sandbox.api import SANDBOX_DISABLE_ENV

PROVIDERS = ("claude", "codex", "gemini", "opencode")
ROLES = ("planner", "implementer", "judge")
STATUS_PROVIDERS = ("claude", "codex")
"""Providers whose CLI has a login status command; the rest report UNKNOWN."""
AUTH_STATES = tuple(agentshim.AuthState)
EXPECTED_STATUS = {
    agentshim.AuthState.KNOWN_OK: AuthStatus.OK,
    agentshim.AuthState.FAILED: AuthStatus.FAILED,
    agentshim.AuthState.UNKNOWN: AuthStatus.UNKNOWN,
}


@pytest.fixture(scope="module")
def home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A throwaway operator HOME: the driver prepares provider state under it."""
    return tmp_path_factory.mktemp("operator-home")


def _launcher(home: Path) -> dict[str, str]:
    return {"PATH": "/usr/bin:/bin", "HOME": str(home), SANDBOX_DISABLE_ENV: "off"}


def _spec(root: Path, provider: str, role: str = "implementer") -> AgentSessionSpec:
    return AgentSessionSpec(
        role=role,
        provider=provider,
        workspace=root,
        policy=AgentExecutionPolicy(require_enforcement=False),
        environment=(("GPU", "0"),),
    )


def _driver(provider: str, fake: FakeExecutor, home: Path) -> AgentShimDriver:
    return AgentShimDriver(
        provider=provider, executor_factory=lambda: fake, launcher_env=lambda: _launcher(home)
    )


def _probe_or_turn(provider: str, *, auth: agentshim.AuthState, installed: bool) -> FakeExecutor:
    """A CLI that answers its probe commands and, for anything else, runs a turn."""
    binary = agentshim.get_provider(provider).profile.binary
    turn = scripted_turn(provider, text="done")

    def pick(request: agentshim.CommandRequest) -> FakeRun:
        args = list(request.argv)[1:]
        probed = probe_run(provider, args, version="9.9.9", auth=auth)
        if probed.returncode != 2:
            return FakeRun(
                stdout=[probed.stdout], stderr=[probed.stderr], returncode=probed.returncode
            )
        return turn

    return FakeExecutor(pick, binaries={binary: f"/usr/bin/{binary}"} if installed else {"": ""})


@given(
    provider=st.sampled_from(PROVIDERS),
    auth=st.sampled_from(AUTH_STATES),
    installed=st.booleans(),
)
def test_the_probe_translates_every_status_the_library_reports(
    provider: str, auth: agentshim.AuthState, *, installed: bool, home: Path
) -> None:
    fake = probe_executor(provider, auth=auth, installed=installed)
    driver = _driver(provider, fake, home)

    readiness = driver.probe_readiness(_spec(Path("/work"), provider))

    assert readiness.provider == provider
    assert readiness.binary_found is installed
    if not installed:
        assert readiness.problem is ReadinessProblem.BINARY_MISSING
        assert readiness.version is None
        return
    assert readiness.version == "1.2.3"
    # Providers with no status command report UNKNOWN whatever the script says.
    has_status = provider in STATUS_PROVIDERS
    expected = EXPECTED_STATUS[auth] if has_status else AuthStatus.UNKNOWN
    assert readiness.auth is expected
    assert (readiness.problem is ReadinessProblem.AUTH_FAILED) == (expected is AuthStatus.FAILED)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_probe_runs_with_the_environment_a_session_turn_gets(
    tmp_path: Path, provider: str, home: Path
) -> None:
    fake = _probe_or_turn(provider, auth=agentshim.AuthState.KNOWN_OK, installed=True)
    driver = _driver(provider, fake, home)
    spec = _spec(tmp_path, provider)

    driver.probe_readiness(spec)
    probe_envs = [dict(request.env) for request in fake.requests]
    driver.create_session(spec).run_turn(AgentTurnRequest(message="go"))
    turn_env = dict(fake.requests[-1].env)

    assert probe_envs
    assert all(env == turn_env for env in probe_envs)
    assert turn_env["GPU"] == "0"


def _client(provider: str, fake: FakeExecutor, home: Path) -> tuple[AgentClient, io.StringIO]:
    log = io.StringIO()
    client = AgentClient(
        _driver(provider, fake, home),
        driver_name="agentshim",
        provider=provider,
        driver_log=AgentDiagnosticLog(log),
        check_readiness=True,
    )
    return client, log


def _invoke(client: AgentClient, root: Path, role: str = "implementer") -> str:
    return client.invoke_text(
        kind=role,
        workspace=root,
        system_prompt="sys",
        user_prompt="go",
        round_label="r1",
    )


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_missing_binary_fails_before_any_turn(tmp_path: Path, provider: str, home: Path) -> None:
    fake = _probe_or_turn(provider, auth=agentshim.AuthState.KNOWN_OK, installed=False)
    client, _ = _client(provider, fake, home)

    with pytest.raises(ProviderNotReadyError) as raised:
        _invoke(client, tmp_path)

    assert raised.value.problem is ReadinessProblem.BINARY_MISSING
    assert raised.value.provider == provider
    assert provider in str(raised.value)
    assert raised.value.retryable is False
    assert fake.requests == []


@pytest.mark.parametrize("provider", STATUS_PROVIDERS)
def test_a_failed_login_fails_before_any_turn_with_the_providers_fix(
    tmp_path: Path, provider: str, home: Path
) -> None:
    fake = _probe_or_turn(provider, auth=agentshim.AuthState.FAILED, installed=True)
    client, _ = _client(provider, fake, home)

    with pytest.raises(ProviderNotReadyError) as raised:
        _invoke(client, tmp_path)

    assert raised.value.problem is ReadinessProblem.AUTH_FAILED
    assert raised.value.detail
    assert raised.value.detail in str(raised.value)
    # Only status commands ran: no turn was launched.
    assert all(
        probe_run(
            provider, list(r.argv)[1:], version="x", auth=agentshim.AuthState.UNKNOWN
        ).returncode
        != 2
        for r in fake.requests
    )


@pytest.mark.parametrize("provider", PROVIDERS)
def test_an_unknown_login_state_proceeds_and_is_logged(
    tmp_path: Path, provider: str, home: Path
) -> None:
    fake = _probe_or_turn(provider, auth=agentshim.AuthState.UNKNOWN, installed=True)
    client, log = _client(provider, fake, home)

    assert _invoke(client, tmp_path)
    logged = log.getvalue()
    assert "[readiness]" in logged
    assert provider in logged


@given(roles=st.lists(st.sampled_from(ROLES), min_size=1, max_size=8))
def test_each_role_is_probed_once_and_a_ready_provider_runs_its_turns(
    roles: list[str], home: Path
) -> None:
    provider = "claude"
    fake = _probe_or_turn(provider, auth=agentshim.AuthState.KNOWN_OK, installed=True)
    client, _ = _client(provider, fake, home)

    for role in roles:
        _invoke(client, Path("/work"), role)

    probes = [
        r
        for r in fake.requests
        if probe_run(
            provider, list(r.argv)[1:], version="x", auth=agentshim.AuthState.UNKNOWN
        ).returncode
        != 2
    ]
    # One version command and one status command per distinct role.
    assert len(probes) == 2 * len(set(roles))


def test_a_failure_is_not_remembered_so_a_fixed_environment_is_rechecked(
    tmp_path: Path, home: Path
) -> None:
    provider = "claude"
    broken = _probe_or_turn(provider, auth=agentshim.AuthState.FAILED, installed=True)
    healthy = _probe_or_turn(provider, auth=agentshim.AuthState.KNOWN_OK, installed=True)
    current = [broken]
    driver = AgentShimDriver(
        provider=provider,
        executor_factory=lambda: current[0],
        launcher_env=lambda: _launcher(home),
    )
    client = AgentClient(driver, driver_name="agentshim", provider=provider, check_readiness=True)

    with pytest.raises(ProviderNotReadyError):
        _invoke(client, tmp_path)
    current[0] = healthy

    assert _invoke(client, tmp_path)
