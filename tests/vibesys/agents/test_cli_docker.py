from __future__ import annotations

from pathlib import Path

import agentshim
import pytest
from tests.support import provider_profiles as fake_profiles

from vibesys.agents import cli_docker

_SHIPPED = ("claude", "codex", "gemini", "opencode")

# Stand-in profiles for the tests whose subject is VibeSys's derivation rather
# than any CLI's declared behaviour. agentshim registers real profiles for all
# four (and for providers VibeSys does not ship), but a test of the derivation
# should fail when the derivation changes, not when a library release edits one
# CLI's install recipe. `TestShippedProfileAssumptions` covers the real
# profiles.
_FAKE_PROFILES = {
    "claude": fake_profiles.profile(
        "claude",
        state_dirs=(".claude", ".claude.json", ".config/claude"),
        auth_env_vars=(
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_CUSTOM_HEADERS",
        ),
        auth_files=(
            ".claude/.credentials.json",
            ".claude/settings.json",
            ".claude/settings.local.json",
            ".claude.json",
        ),
    ),
    "codex": fake_profiles.profile(
        "codex",
        state_dirs=(".codex", ".config/codex"),
        auth_env_vars=("OPENAI_API_KEY", "OPENAI_BASE_URL"),
        auth_files=(".codex/auth.json", ".codex/config.toml"),
    ),
    "gemini": fake_profiles.profile(
        "gemini",
        state_dirs=(".gemini", ".config/gemini"),
        auth_env_vars=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        auth_files=(
            ".gemini/oauth_creds.json",
            ".gemini/google_accounts.json",
            ".gemini/settings.json",
            ".gemini/.env",
        ),
    ),
    "opencode": fake_profiles.profile(
        "opencode",
        state_dirs=(".local/share/opencode", ".config/opencode"),
        auth_env_vars=(),
        auth_files=(
            ".local/share/opencode/auth.json",
            ".config/opencode/opencode.json",
            ".config/opencode/opencode.jsonc",
            ".config/opencode/config.json",
            ".config/opencode/config.jsonc",
            ".config/opencode/.env",
        ),
    ),
}


@pytest.fixture
def fake_profiles_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer every profile lookup from the fakes above, not from agentshim."""
    fake_profiles.install(monkeypatch, _FAKE_PROFILES)


class TestAuthPaths:
    """Which provider state files are staged into an editor container."""

    def test_stages_exactly_the_profiles_declared_auth_files(
        self,
        fake_profiles_installed: None,
    ) -> None:
        """``auth_paths`` derives entirely from ``ProviderProfile.auth_files``.

        Expectations come from the fake profile itself, not a second
        hand-typed list, so this fails only when the derivation rule changes.
        """
        del fake_profiles_installed
        home = Path.home()

        for provider in _SHIPPED:
            expected = [
                (auth_file, f"/home/agent/{auth_file}")
                for auth_file in _FAKE_PROFILES[provider].auth_files
            ]
            staged = [
                (spec.host_path.relative_to(home).as_posix(), spec.container_path)
                for spec in cli_docker.auth_paths(provider)
            ]
            assert staged == expected

    def test_never_stages_a_bulk_runtime_root(self, fake_profiles_installed: None) -> None:
        del fake_profiles_installed
        configured = {
            spec.host_path for provider in _SHIPPED for spec in cli_docker.auth_paths(provider)
        }

        assert configured.isdisjoint(
            {
                Path.home() / ".claude",
                Path.home() / ".gemini",
                Path.home() / ".codex",
                Path.home() / ".local" / "share" / "opencode",
                Path.home() / ".config" / "opencode",
            }
        )

    def test_stages_nothing_when_the_profile_declares_no_auth_files(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_profiles.install(
            monkeypatch,
            {"fixture": fake_profiles.profile("fixture", state_dirs=(".fixture-cache",))},
        )

        assert cli_docker.auth_paths("fixture") == []

    def test_rejects_a_provider_agentshim_does_not_register(self) -> None:
        with pytest.raises(ValueError, match="unregistered-provider"):
            cli_docker.auth_paths("unregistered-provider")


class TestAuthImport:
    """Staged state is mounted read-only and copied into writable storage."""

    def test_mounts_and_names_each_existing_leaf_for_writable_import(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text('{"synthetic": true}\n')
        (codex_home / "config.toml").write_text("model = 'synthetic'\n")
        (tmp_path / ".claude.json").write_text('{"synthetic": true}\n')
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
        fake_profiles.install(
            monkeypatch,
            {
                "fixture": fake_profiles.profile(
                    "fixture",
                    state_dirs=(".codex", ".claude.json"),
                    auth_files=(".codex/auth.json", ".codex/config.toml", ".claude.json"),
                )
            },
        )

        assert cli_docker.auth_bind_mounts("fixture") == [
            (str(codex_home / "auth.json"), "/opt/vibesys-auth/0", True),
            (str(codex_home / "config.toml"), "/opt/vibesys-auth/1", True),
            (str(tmp_path / ".claude.json"), "/opt/vibesys-auth/2", True),
        ]
        # `auth_copy_paths` hands these straight to `DockerSandbox(auth_files=...)`,
        # which copies each pair in as a start-time step; no shell recipe.
        assert cli_docker.auth_copy_paths("fixture") == [
            ("/opt/vibesys-auth/0", "/home/agent/.codex/auth.json"),
            ("/opt/vibesys-auth/1", "/home/agent/.codex/config.toml"),
            ("/opt/vibesys-auth/2", "/home/agent/.claude.json"),
        ]

    def test_keeps_staging_indexes_stable_when_a_host_file_is_absent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text("model = 'synthetic'\n")
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
        fake_profiles.install(
            monkeypatch,
            {
                "fixture": fake_profiles.profile(
                    "fixture",
                    state_dirs=(".codex",),
                    auth_files=(".codex/auth.json", ".codex/config.toml"),
                )
            },
        )

        # The mount index is the position in the full list, so a missing
        # auth.json must not renumber the staging path of config.toml.
        assert cli_docker.auth_bind_mounts("fixture") == [
            (str(codex_home / "config.toml"), "/opt/vibesys-auth/1", True),
        ]
        assert cli_docker.auth_copy_paths("fixture") == [
            ("/opt/vibesys-auth/1", "/home/agent/.codex/config.toml"),
        ]


class TestAuthEnvVars:
    """Credential variables come from the profile; model selection never does."""

    def test_matches_the_shipped_claude_profile(self) -> None:
        assert cli_docker.auth_env_vars("claude") == (
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_CUSTOM_HEADERS",
        )

    def test_carries_no_model_selection_variable(self, fake_profiles_installed: None) -> None:
        del fake_profiles_installed
        forwarded = {name for provider in _SHIPPED for name in cli_docker.auth_env_vars(provider)}

        # VibeSys owns per-role model selection; a host export must not
        # override it inside the container.
        assert forwarded.isdisjoint({"ANTHROPIC_MODEL", "OPENAI_MODEL", "GEMINI_MODEL"})

    def test_forwards_only_variables_the_host_actually_set(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-value")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.invalid/v1")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
        monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
        monkeypatch.setenv("ANTHROPIC_MODEL", "host-selected-model")

        assert cli_docker.auth_env_passthrough("claude") == {
            "ANTHROPIC_AUTH_TOKEN": "token-value",
            "ANTHROPIC_BASE_URL": "https://proxy.invalid/v1",
        }

    def test_is_empty_without_host_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in cli_docker.auth_env_vars("claude"):
            monkeypatch.delenv(name, raising=False)

        assert cli_docker.auth_env_passthrough("claude") == {}

    def test_rejects_a_provider_agentshim_does_not_register(self) -> None:
        with pytest.raises(ValueError, match="unregistered-provider"):
            cli_docker.auth_env_passthrough("unregistered-provider")


class TestShippedProfileAssumptions:
    """The real agentshim profiles, run through the tables above.

    Nothing here is monkeypatched: these pin that what the four shipped
    providers actually declare still satisfies what VibeSys derives from it, so
    a library release that renames a state directory or adds a
    model-selection variable fails here rather than at container start.
    """

    @pytest.mark.parametrize("provider", _SHIPPED)
    def test_auth_paths_mirrors_the_profiles_own_auth_files(self, provider: str) -> None:
        profile = agentshim.get_provider(provider).profile
        home = Path.home()

        assert [
            (spec.host_path, spec.container_path) for spec in cli_docker.auth_paths(provider)
        ] == [(home / auth_file, f"/home/agent/{auth_file}") for auth_file in profile.auth_files]
        # A provider that declares no auth files starts its container CLI
        # logged out.
        assert profile.auth_files
        # Every declared auth file lies inside a declared state directory;
        # agentshim's own `test_conventions.py` pins that per provider, so
        # this only checks that VibeSys's read seam still sees it that way.
        for auth_file in profile.auth_files:
            assert any(
                auth_file == state_dir or auth_file.startswith(f"{state_dir}/")
                for state_dir in profile.state_dirs
            ), auth_file

    @pytest.mark.parametrize("provider", _SHIPPED)
    def test_auth_env_vars_carry_credentials_and_no_model_selection(self, provider: str) -> None:
        forwarded = cli_docker.auth_env_vars(provider)

        assert forwarded == agentshim.get_provider(provider).profile.auth_env_vars
        # VibeSys owns per-role model selection, so no shipped profile may hand
        # the container a host override of it (see `auth_env_vars`).
        assert [name for name in forwarded if name.endswith("_MODEL")] == []


def test_docker_provider_env_covers_every_provider_vibesys_ships() -> None:
    assert set(cli_docker.DOCKER_PROVIDER_ENV) == set(_SHIPPED)
    # The agent image runs the CLI as the non-root ``agent`` user, so Claude's
    # root-only IS_SANDBOX=1 escape hatch (a profile.container_env entry) is no
    # longer folded in.
    assert "IS_SANDBOX" not in cli_docker.DOCKER_PROVIDER_ENV["claude"]
    assert all(
        env["PYTHONPATH"] == "/opt/vibesys" for env in cli_docker.DOCKER_PROVIDER_ENV.values()
    )
