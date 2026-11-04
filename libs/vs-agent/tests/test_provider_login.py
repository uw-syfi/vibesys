"""A provider CLI's login on a machine: installed, signed in, how, never the secret.

Everything is scripted with ``agentshim.testing``, so no CLI is needed.
"""

from __future__ import annotations

from pathlib import Path

import agentshim
import pytest
from agentshim.testing import probe_executor
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api import (
    LoginKind,
    LoginState,
    ProviderLogin,
    harnesses,
    provider_login,
    provider_login_command,
)

PROVIDERS = sorted({harness.provider for harness in harnesses()})
STATUS_PROVIDERS = ("claude", "codex")
"""Providers whose CLI has a login status command; the rest report an unknown login."""
HOME = Path("/home/user")


def _login(
    provider: str, auth: agentshim.AuthState, env: dict[str, str] | None = None
) -> ProviderLogin:
    return provider_login(
        provider, env=env or {}, home=HOME, executor=probe_executor(provider, auth=auth)
    )


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_missing_binary_is_a_result_with_an_unknown_login(provider: str) -> None:
    result = provider_login(
        provider, env={}, home=HOME, executor=probe_executor(provider, installed=False)
    )

    assert not result.installed
    assert result.state is LoginState.UNKNOWN
    assert result.credential is None


@pytest.mark.parametrize("provider", STATUS_PROVIDERS)
@pytest.mark.parametrize("auth", list(agentshim.AuthState))
def test_the_login_state_is_the_clis_own_answer(provider: str, auth: agentshim.AuthState) -> None:
    result = _login(provider, auth)

    expected = {
        agentshim.AuthState.KNOWN_OK: LoginState.SIGNED_IN,
        agentshim.AuthState.FAILED: LoginState.SIGNED_OUT,
        agentshim.AuthState.UNKNOWN: LoginState.UNKNOWN,
    }[auth]
    assert result.installed
    assert result.state is expected
    assert (result.credential is not None) <= (result.state is LoginState.SIGNED_IN)


@given(secret=st.text(alphabet="abcdefghijklmnop0123456789", min_size=12, max_size=40))
def test_an_api_key_is_named_by_its_variable_and_the_secret_never_appears(secret: str) -> None:
    env = {"CODEX_API_KEY": secret}

    result = provider_login(
        "codex",
        env=env,
        home=HOME,
        executor=probe_executor("codex", auth=agentshim.AuthState.KNOWN_OK),
    )

    assert result.state is LoginState.SIGNED_IN
    if result.credential is not None and result.credential.kind is LoginKind.API_KEY:
        assert result.credential.variable == "CODEX_API_KEY"
    assert secret not in repr(result)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_variables_that_sign_a_cli_in_are_its_declared_api_key_sources(
    provider: str,
) -> None:
    result = provider_login(
        provider, env={}, home=HOME, executor=probe_executor(provider, installed=False)
    )

    declared = {
        source.name
        for source in agentshim.get_provider(provider).profile.credential_sources
        if isinstance(source, agentshim.EnvCredential)
        and source.kind is agentshim.CredentialKind.API_KEY
    }
    assert set(result.api_key_variables) == declared


def test_an_unknown_provider_is_refused() -> None:
    with pytest.raises(ValueError, match="not-a-provider"):
        provider_login("not-a-provider", env={}, home=HOME)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_login_command_is_the_clis_own_and_a_device_login_never_needs_a_terminal(
    provider: str,
) -> None:
    command = provider_login_command(provider)
    profile = agentshim.get_provider(provider).profile

    if profile.login is None:
        assert command is None
        return
    assert command is not None
    assert command.argv[0] == profile.binary
    device = profile.login.device_argv is not None
    assert command.argv == profile.login.command(profile.binary, device=device)
    assert command.needs_terminal == (profile.login.needs_terminal and not device)
    assert command.line.split()[0] == profile.binary


def test_a_provider_agentshim_does_not_register_has_no_login_command() -> None:
    with pytest.raises(ValueError, match="unknown"):
        provider_login_command("no-such-provider")
