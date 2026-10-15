"""What a CLI agent's container is given to log in with when the run has no auth policy.

The CLI logs in the way the launching shell configured it, and agentshim
plans exactly what a confined copy needs to do the same. The container gets
that plan and nothing else of the operator's auth state: not their other
logins, and not their settings, whose hooks, plugins and helper commands name
host paths and can carry credentials of their own. Only a host whose login
agentshim cannot plan gets its auth state forwarded as it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import agentshim
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_agent.api import CliAuthPlan, CredentialKind, HostAuthUnplannable, cli_auth_delivery
from vs_agent.api.testing import fake_profiles
from vs_sim.api.testing import examples

# A stand-in CLI with the shape every classified provider has: environment
# credentials ahead of a login file in the precedence order, an endpoint
# variable with a companion, and settings files beside the login.
_API_VAR = "FIXTURE_API"
_GATEWAY_VAR = "FIXTURE_GATEWAY"
_PLAN_VAR = "FIXTURE_PLAN_VAR"
_URL = "FIXTURE_BASE_URL"
_HEADERS = "FIXTURE_HEADERS"
_LOGIN = ".fixture/login.json"
_SETTINGS = (".fixture/settings.json", ".fixture.json")
_ENV_SOURCES = (
    (_GATEWAY_VAR, CredentialKind.API_KEY),
    (_API_VAR, CredentialKind.API_KEY),
    (_PLAN_VAR, CredentialKind.SUBSCRIPTION),
)
_AUTH_ENV = (*(name for name, _ in _ENV_SOURCES), _URL, _HEADERS)


def _fixture_profile(*, classified: bool) -> agentshim.ProviderProfile:
    return fake_profiles.profile(
        "fixture",
        state_dirs=(".fixture", ".fixture.json"),
        auth_env_vars=_AUTH_ENV,
        auth_files=(_LOGIN, *_SETTINGS),
        credential_files=(_LOGIN,),
        credential_sources=(
            (
                *(fake_profiles.env_credential(name, kind) for name, kind in _ENV_SOURCES),
                fake_profiles.file_credential(_LOGIN, CredentialKind.SUBSCRIPTION),
            )
            if classified
            else ()
        ),
        endpoint=fake_profiles.endpoint(_URL, _HEADERS) if classified else None,
    )


@dataclass(frozen=True)
class _Host:
    env: dict[str, str]
    files: frozenset[str]
    classified: bool


_env_value = st.sampled_from([None, "", "  ", "secret-value"])


@st.composite
def _hosts(draw: st.DrawFn) -> _Host:
    env: dict[str, str] = {}
    for name in (*(name for name, _ in _ENV_SOURCES), _HEADERS):
        value = draw(_env_value)
        if value is not None:
            env[name] = f"{value}-{name}" if value.strip() else value
    url = draw(st.sampled_from([None, "", "https://gateway.invalid/v1", "not-a-url"]))
    if url is not None:
        env[_URL] = url
    files = draw(st.frozensets(st.sampled_from((_LOGIN, *_SETTINGS))))
    return _Host(env, files, classified=draw(st.booleans()))


def _plant(home: Path, files: frozenset[str]) -> None:
    for leaf in files:
        (home / leaf).parent.mkdir(parents=True, exist_ok=True)
        (home / leaf).write_text("{}")


def _isolate(monkeypatch: pytest.MonkeyPatch, home: Path, names: tuple[str, ...]) -> None:
    """Make *home* the host home and clear every auth variable the test does not set."""
    monkeypatch.setenv("HOME", str(home))
    for name in names:
        monkeypatch.delenv(name, raising=False)


@settings(max_examples=examples(pr=60, nightly=600), deadline=None)
@given(host=_hosts())
def test_a_host_login_agentshim_can_plan_is_delivered_alone(
    tmp_path_factory: pytest.TempPathFactory, host: _Host
) -> None:
    home = tmp_path_factory.mktemp("home")
    _plant(home, host.files)
    profile = _fixture_profile(classified=host.classified)
    forwarded = {name: value for name, value in host.env.items() if value.strip()}
    try:
        expected: agentshim.AuthPlan | None = agentshim.host_auth_plan(
            profile, env=host.env, home=home
        )
    except agentshim.HostAuthUnplannableError:
        expected = None
    with pytest.MonkeyPatch.context() as monkeypatch:
        _isolate(monkeypatch, home, _AUTH_ENV)
        for name, value in host.env.items():
            monkeypatch.setenv(name, value)
        fake_profiles.install(monkeypatch, {"fixture": profile})
        if expected is None and not host.files and not forwarded:
            with pytest.raises(ValueError, match="no 'fixture' CLI authentication"):
                cli_auth_delivery("fixture", None)
            return
        delivery = cli_auth_delivery("fixture", None)

    staged = {Path(host_path) for host_path, _, _ in delivery.bind_mounts}
    if expected is not None:
        assert isinstance(delivery.login, CliAuthPlan)
        assert dict(delivery.env) == dict(expected.env)
        assert staged == {staged_file.host_path for staged_file in expected.files}
        # Exactly one credential, and never a settings file beside it.
        assert not staged & {home / leaf for leaf in _SETTINGS}
        assert expected.source in delivery.describe()
    else:
        assert isinstance(delivery.login, HostAuthUnplannable)
        assert dict(delivery.env) == forwarded
        assert staged == {home / leaf for leaf in host.files}
    # The endpoint is named on purpose; credentials and headers never are.
    description = delivery.describe()
    for name, value in forwarded.items():
        assert name == _URL or value not in description


_SHIPPED = ("claude", "codex", "gemini", "opencode")


def _env_sources(provider: str) -> list[tuple[str, str]]:
    profile = agentshim.get_provider(provider).profile
    return [
        (provider, source.name)
        for source in profile.credential_sources
        if isinstance(source, agentshim.EnvCredential)
    ]


@pytest.mark.parametrize(
    ("provider", "variable"),
    [pair for provider in _SHIPPED for pair in _env_sources(provider)],
)
def test_no_shipped_provider_stages_a_host_login_beside_an_env_credential(
    provider: str, variable: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every shipped CLI whose env credential would win gets no host auth file at all."""
    profile = agentshim.get_provider(provider).profile
    _plant(tmp_path, frozenset(profile.credential_files))
    _isolate(monkeypatch, tmp_path, profile.auth_env_vars)
    monkeypatch.setenv(variable, "synthetic-credential")

    delivery = cli_auth_delivery(provider, None)

    assert delivery.bind_mounts == ()
    assert delivery.copy_paths == ()
    assert dict(delivery.env) == {variable: "synthetic-credential"}


_CLAUDE = agentshim.get_provider("claude").profile


@pytest.mark.parametrize(
    ("variable", "base_url"),
    [
        # The reported case: a gateway token plus base URL on a host also logged in by OAuth.
        ("ANTHROPIC_AUTH_TOKEN", "https://gateway.invalid/anthropic"),
        # The CLI sends a subscription token to a custom endpoint too.
        ("CLAUDE_CODE_OAUTH_TOKEN", "https://gateway.invalid/anthropic"),
        # A base URL the CLI cannot use reaches the container as it is, so the
        # agent fails the way the host CLI would instead of using the host login.
        ("ANTHROPIC_API_KEY", "not-a-url"),
    ],
)
def test_a_claude_env_credential_gets_no_host_login_or_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str, base_url: str
) -> None:
    _plant(tmp_path, frozenset(_CLAUDE.auth_files))
    _isolate(monkeypatch, tmp_path, _CLAUDE.auth_env_vars)
    monkeypatch.setenv(variable, "synthetic-credential")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", base_url)

    delivery = cli_auth_delivery("claude", None)

    assert delivery.bind_mounts == ()
    assert delivery.copy_paths == ()
    assert dict(delivery.env) == {variable: "synthetic-credential", "ANTHROPIC_BASE_URL": base_url}
    description = delivery.describe()
    assert variable in description
    assert ".claude/.credentials.json" in description
    assert "synthetic-credential" not in description


def test_a_claude_file_login_shares_only_the_login_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The login is shared writable at its final path; the host's settings stay home."""
    _plant(tmp_path, frozenset(_CLAUDE.auth_files))
    _isolate(monkeypatch, tmp_path, _CLAUDE.auth_env_vars)

    delivery = cli_auth_delivery("claude", None)

    assert delivery.bind_mounts == (
        (
            str(tmp_path / ".claude/.credentials.json"),
            "/home/agent/.claude/.credentials.json",
            False,
        ),
    )
    assert delivery.copy_paths == ()
    assert dict(delivery.env) == {}


def test_a_host_login_agentshim_cannot_plan_forwards_the_host_state_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant(tmp_path, frozenset(_CLAUDE.auth_files))
    (tmp_path / ".claude/settings.json").write_text('{"apiKeyHelper": "synthetic-helper"}')
    _isolate(monkeypatch, tmp_path, _CLAUDE.auth_env_vars)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-credential")

    delivery = cli_auth_delivery("claude", None)

    assert isinstance(delivery.login, HostAuthUnplannable)
    assert {Path(host) for host, _, _ in delivery.bind_mounts} == {
        tmp_path / leaf for leaf in _CLAUDE.auth_files
    }
    assert dict(delivery.env) == {"CLAUDE_CODE_OAUTH_TOKEN": "synthetic-credential"}
    assert "apiKeyHelper" in delivery.describe()
    assert "synthetic" not in delivery.describe()
