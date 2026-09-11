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
        container_install=(
            "apt-get update && apt-get install -y --no-install-recommends curl ca-certificates",
            "curl -fsSL https://claude.ai/install.sh | bash",
            "ln -sf /root/.local/bin/claude /usr/local/bin/claude",
        ),
    ),
    "codex": fake_profiles.profile(
        "codex",
        state_dirs=(".codex", ".config/codex"),
        auth_env_vars=("OPENAI_API_KEY", "OPENAI_BASE_URL"),
        auth_files=(".codex/auth.json", ".codex/config.toml"),
        container_install=(
            "curl -fsSL https://nodejs.org/install | bash",
            "npm install -g @openai/codex",
        ),
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
        container_install=("npm install -g @google/gemini-cli",),
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
        container_install=("curl -fsSL https://opencode.ai/install | bash",),
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

    def test_mounts_and_copies_each_existing_leaf_into_writable_storage(
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
        assert cli_docker.auth_copy_commands("fixture") == [
            "mkdir -p /home/agent/.codex && cp -a /opt/vibesys-auth/0 /home/agent/.codex/auth.json",
            "mkdir -p /home/agent/.codex && cp -a /opt/vibesys-auth/1 /home/agent/.codex/config.toml",
            "mkdir -p /home/agent && cp -a /opt/vibesys-auth/2 /home/agent/.claude.json",
        ]
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
        assert cli_docker.auth_copy_commands("fixture") == [
            "mkdir -p /home/agent/.codex && cp -a /opt/vibesys-auth/1 /home/agent/.codex/config.toml",
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


class TestDockerInitCommands:
    """The provider recipe from the profile, plus VibeSys's own toolchain."""

    def test_runs_the_profile_recipe_before_the_common_tooling(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_profiles.install(
            monkeypatch,
            {
                "fixture": fake_profiles.profile(
                    "fixture",
                    container_install=("install-the-cli", "link-the-binary"),
                ),
                "bare": fake_profiles.profile("bare"),
            },
        )

        commands = cli_docker.docker_init_commands("fixture")

        assert commands[:2] == ["install-the-cli", "link-the-binary"]
        # The tail is the same for every provider: it is VibeSys's own
        # toolchain, not anything the profile asked for.
        assert commands[2:] == cli_docker.docker_init_commands("bare")

    @pytest.mark.parametrize("provider", _SHIPPED)
    def test_installs_only_mcp_v1(self, provider: str, fake_profiles_installed: None) -> None:
        del fake_profiles_installed
        commands = cli_docker.docker_init_commands(provider)

        assert any("command -v pip3" in command for command in commands)
        assert (
            "PIP_BREAK_SYSTEM_PACKAGES=1 python3 -m pip install --quiet 'mcp>=1.0,<2'" in commands
        )

    @pytest.mark.parametrize("provider", _SHIPPED)
    def test_installs_the_pinned_rust_toolchain(
        self,
        provider: str,
        fake_profiles_installed: None,
    ) -> None:
        del fake_profiles_installed
        commands = cli_docker.docker_init_commands(provider)
        rust_install = next(command for command in commands if "rustup-init.sh" in command)

        assert "command -v cargo" in rust_install
        assert f"--default-toolchain {cli_docker.RUST_DOCKER_TOOLCHAIN_VERSION}" in rust_install
        assert "--profile minimal" in rust_install
        assert "--component rustfmt --component clippy" in rust_install
        assert "ln -sf /root/.cargo/bin/* /usr/local/bin/" in rust_install
        assert cli_docker.RUST_DOCKER_TOOLCHAIN_VERSION == "1.92.0"

    def test_hardens_the_single_shot_apt_bootstrap_the_claude_profile_ships(self) -> None:
        recipe = agentshim.get_provider("claude").profile.container_install
        assert any("apt-get" in step for step in recipe), recipe

        commands = cli_docker.docker_init_commands("claude")

        # Ubuntu mirrors reachable from our hosts fail a single-shot
        # `apt-get update` often enough to break container start, so every
        # apt-get that survives into the recipe retries. When the library
        # rewords its bootstrap step the override stops matching and this
        # fails, instead of silently reverting the hardening.
        apt_steps = [step for step in commands if "apt-get" in step]
        assert apt_steps
        assert all("apt retry $i" in step for step in apt_steps)

    def test_runs_a_shared_step_once_when_the_recipe_already_has_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_profiles.install(
            monkeypatch,
            {
                "fixture": fake_profiles.profile(
                    "fixture",
                    container_install=(
                        "apt-get update && apt-get install -y --no-install-recommends "
                        "curl ca-certificates",
                        "install-the-cli",
                    ),
                )
            },
        )

        commands = cli_docker.docker_init_commands("fixture")

        assert len(commands) == len(set(commands))
        assert commands[1] == "install-the-cli"

    def test_pins_an_unpinned_codex_cli_install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_profiles.install(
            monkeypatch,
            {
                "codex": fake_profiles.profile(
                    "codex",
                    container_install=("npm install -g @openai/codex",),
                )
            },
        )

        commands = cli_docker.docker_init_commands("codex")

        assert commands[0] == (
            f"npm install -g --include=optional @openai/codex@{cli_docker.CODEX_DOCKER_CLI_VERSION}"
        )
        assert cli_docker.CODEX_DOCKER_CLI_VERSION == "0.144.4"

    def test_leaves_a_codex_version_the_profile_already_pinned(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_profiles.install(
            monkeypatch,
            {
                "codex": fake_profiles.profile(
                    "codex",
                    container_install=("npm install -g @openai/codex@9.9.9",),
                )
            },
        )

        assert cli_docker.docker_init_commands("codex")[0] == ("npm install -g @openai/codex@9.9.9")

    def test_rejects_a_provider_agentshim_does_not_register(self) -> None:
        with pytest.raises(ValueError, match="unregistered-provider"):
            cli_docker.docker_init_commands("unregistered-provider")


class TestShippedProfileAssumptions:
    """The real agentshim profiles, run through the tables above.

    Nothing here is monkeypatched: these pin that what the four shipped
    providers actually declare still satisfies what VibeSys derives from it, so
    a library release that renames a state directory, adds a model-selection
    variable, or rewrites an install recipe fails here rather than at container
    start.
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

    @pytest.mark.parametrize("provider", _SHIPPED)
    def test_recipe_ends_with_the_common_tooling_it_does_not_already_run(
        self,
        provider: str,
    ) -> None:
        common = cli_docker._COMMON_DOCKER_TOOLING_INSTALL  # noqa: SLF001
        commands = cli_docker.docker_init_commands(provider)
        split = len(commands)
        while split and commands[split - 1] in common:
            split -= 1
        head, tail = commands[:split], commands[split:]

        # The trailing common steps keep their order and gain nothing, and the
        # ones missing from the tail are exactly the ones the profile recipe
        # already runs.
        assert tail == [step for step in common if step in tail]
        assert all(step in head for step in common if step not in tail)
        assert len(commands) == len(set(commands))

    @pytest.mark.parametrize("provider", _SHIPPED)
    def test_every_npm_step_runs_after_node_is_on_the_path(self, provider: str) -> None:
        commands = cli_docker.docker_init_commands(provider)

        # The node bootstrap is inside the recipe, ahead of the common tooling,
        # so a profile that drops its `command -v node` guard would run npm on
        # an image without node.
        for index, command in enumerate(commands):
            if "npm " not in command:
                continue
            assert any("command -v node" in earlier for earlier in commands[: index + 1]), command

    def test_codex_installs_the_verified_cli_version(self) -> None:
        commands = cli_docker.docker_init_commands("codex")

        # Either the profile pins this version or `_pin_codex_cli` supplies it;
        # a profile that starts pinning a different one fails here instead of
        # silently changing the editor mid-campaign.
        pin = f"@openai/codex@{cli_docker.CODEX_DOCKER_CLI_VERSION}"
        assert any(pin in command for command in commands), commands


def test_docker_provider_env_covers_every_provider_vibesys_ships() -> None:
    assert set(cli_docker.DOCKER_PROVIDER_ENV) == set(_SHIPPED)
    # The agent image runs the CLI as the non-root ``agent`` user, so Claude's
    # root-only IS_SANDBOX=1 escape hatch (a profile.container_env entry) is no
    # longer folded in.
    assert "IS_SANDBOX" not in cli_docker.DOCKER_PROVIDER_ENV["claude"]
    assert all(
        env["PYTHONPATH"] == "/opt/vibesys" for env in cli_docker.DOCKER_PROVIDER_ENV.values()
    )
