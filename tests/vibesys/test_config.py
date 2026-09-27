import os
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from vibesys.config import _load_dotenv_file, load_config


class TestLoadConfigValid:
    @patch.dict(os.environ, {}, clear=False)
    def test_full_config(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text("""\
[model]
name = "claude-sonnet-4-6"

[thinking]
level = "medium"
""")
        config = load_config(cfg_file)
        assert config.model.name == "claude-sonnet-4-6"
        assert config.thinking.level == "medium"
        assert config.thinking.budget is None

    def test_minimal_config(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text('[model]\nname = "claude-sonnet-4-6"\n')
        config = load_config(cfg_file)
        assert config.model.name == "claude-sonnet-4-6"
        assert config.thinking.level is None
        assert config.thinking.budget is None
        assert config.repository.owner is None
        assert config.repository.visibility == "private"

    def test_plugin_role_agent_models_are_parsed(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text(
            """\
[model]
name = "gpt-5.6-sol"

[thinking]
level = "high"

[agent]
backend = "cli"
cli_provider = "codex"

[agent.roles.orchestrator]
model = "gpt-5.6-sol"
reasoning_effort = "xhigh"

[agent.roles.implementer]
model = "gpt-5.6-luna"
reasoning_effort = "xhigh"
"""
        )

        config = load_config(cfg_file)

        assert config.agent.roles["orchestrator"].model == "gpt-5.6-sol"
        assert config.agent.roles["orchestrator"].reasoning_effort == "xhigh"
        assert config.agent.roles["implementer"].model == "gpt-5.6-luna"
        assert config.agent.roles["implementer"].reasoning_effort == "xhigh"


class TestLoadConfigErrors:
    def test_missing_model_name(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text("[model]\nprovider = 'vertex-ai'\n")
        with pytest.raises(ValueError, match="name"):
            load_config(cfg_file)

    def test_missing_file(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_config(Path("/nonexistent/agent.toml"))

    def test_invalid_toml(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text("not valid toml [[[")
        with pytest.raises(tomllib.TOMLDecodeError):
            load_config(cfg_file)

    def test_removed_model_provider_is_rejected(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text("""\
[model]
name = "claude-sonnet-4-6"
provider = "openai"
""")
        with pytest.raises(ValueError, match="provider"):
            load_config(cfg_file)

    def test_removed_providers_section_is_rejected(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text("""\
[model]
name = "gpt-5.4"

[providers.openai]
""")
        with pytest.raises(ValueError, match="providers"):
            load_config(cfg_file)

    def test_removed_feature_flags_section_is_rejected(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text("""\
[model]
name = "claude-sonnet-4-6"

[feature_flags]
example_feature = true
""")
        with pytest.raises(ValueError, match="feature_flags"):
            load_config(cfg_file)


class TestLoadConfigStrict:
    """Unknown sections/keys are rejected (fail-fast), not silently dropped."""

    def test_unknown_top_level_section_rejected(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text('[model]\nname = "claude-sonnet-4-6"\n\n[bogus]\nx = 1\n')
        with pytest.raises(ValueError, match="bogus"):
            load_config(cfg_file)

    def test_unknown_key_in_known_section_rejected(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text('[model]\nname = "claude-sonnet-4-6"\n\n[agent]\ncli_modle = "x"\n')
        with pytest.raises(ValueError, match="cli_modle"):
            load_config(cfg_file)

    def test_unknown_backend_rejected(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text('[model]\nname = "claude-sonnet-4-6"\n\n[backend]\nname = "tpu"\n')
        with pytest.raises(ValueError, match="tpu"):
            load_config(cfg_file)

    def test_removed_cli_model_key_rejected(self, tmp_path: Path) -> None:
        # [agent].cli_model was removed in favour of [model].name driving the
        # CLI tool directly; stale configs that still set it must error rather
        # than be silently ignored.
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text(
            '[model]\nname = "gpt-5.4"\n\n[agent]\ncli_provider = "codex"\ncli_model = "gpt-5-codex"\n'
        )
        with pytest.raises(ValueError, match="cli_model"):
            load_config(cfg_file)


class TestLoadDotenvFile:
    def test_load_dotenv_file_parses_values(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("""\
# comment
ANTHROPIC_API_KEY=anthropic-key
OPENAI_API_KEY='openai-key'
export GOOGLE_API_KEY="google-key"
EMPTY=
""")
        with patch.dict("os.environ", {}, clear=True):
            _load_dotenv_file(env_file)
            assert os.environ["ANTHROPIC_API_KEY"] == "anthropic-key"
            assert os.environ["OPENAI_API_KEY"] == "openai-key"
            assert os.environ["GOOGLE_API_KEY"] == "google-key"
            assert os.environ["EMPTY"] == ""

    def test_load_dotenv_file_does_not_override_existing(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("OPENAI_API_KEY=from-file")
        with patch.dict("os.environ", {"OPENAI_API_KEY": "existing"}, clear=True):
            _load_dotenv_file(env_file)
            assert os.environ["OPENAI_API_KEY"] == "existing"

    def test_load_dotenv_file_strips_inline_comments(self, tmp_path: Path) -> None:
        # python-dotenv strips trailing inline comments on unquoted values, but
        # preserves a '#' inside quotes (the prior hand-rolled parser did neither).
        env_file = tmp_path / ".env"
        env_file.write_text('PLAIN=value # trailing comment\nQUOTED="val # hash"\n')
        with patch.dict("os.environ", {}, clear=True):
            _load_dotenv_file(env_file)
            assert os.environ["PLAIN"] == "value"
            assert os.environ["QUOTED"] == "val # hash"

    def test_load_dotenv_file_missing_file_is_noop(self, tmp_path: Path) -> None:
        with patch.dict("os.environ", {}, clear=True):
            _load_dotenv_file(tmp_path / "does-not-exist.env")  # no error


class TestLoadConfigThinking:
    @pytest.mark.parametrize("budget", [-1, 0, 2048])
    def test_thinking_budget_parsed(self, tmp_path: Path, budget: object) -> None:
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text(f"""\
[model]
name = "gemini-2.5-pro"

[thinking]
budget = {budget}
""")
        config = load_config(cfg_file)
        assert config.thinking.level is None
        assert config.thinking.budget == budget

    def test_thinking_level_and_budget_are_rejected(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text("""\
[model]
name = "gemini-2.5-pro"

[thinking]
level = "high"
budget = 2048
""")

        with pytest.raises(ValueError, match=r"thinking\.level.*thinking\.budget"):
            load_config(cfg_file)

    def test_thinking_budget_below_dynamic_sentinel_is_rejected(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text("""\
[model]
name = "gemini-2.5-pro"

[thinking]
budget = -2
""")

        with pytest.raises(ValueError, match="budget"):
            load_config(cfg_file)


class TestLoadConfigAgentSection:
    def test_agent_section_preserved(self, tmp_path: Path) -> None:
        # The [agent] table drives build_agent_client (cli_timeout, backend,
        # cli_provider). load_config must carry it through; the previous
        # allowlist loader silently dropped it.
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text("""\
[model]
name = "claude-sonnet-4-6"

[agent]
driver = "omnigent"
backend = "cli"
cli_provider = "claude"
cli_timeout = 1800
""")
        config = load_config(cfg_file)
        assert config.agent.driver == "omnigent"
        assert config.agent.cli_timeout == 1800
        assert config.agent.backend == "cli"
        assert config.agent.cli_provider == "claude"

    def test_agent_section_defaults_to_empty(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text('[model]\nname = "claude-sonnet-4-6"\n')
        config = load_config(cfg_file)
        assert config.agent.driver is None
        assert config.agent.backend is None
        assert config.agent.cli_provider is None
        assert config.agent.cli_timeout is None

    def test_unknown_agent_driver_is_rejected(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text("""\
[model]
name = "claude-sonnet-4-6"

[agent]
driver = "unknown"
""")

        with pytest.raises(ValueError, match="driver"):
            load_config(cfg_file)

    @pytest.mark.parametrize("cli_timeout", [0, -1])
    def test_non_positive_cli_timeout_is_rejected(
        self, tmp_path: Path, cli_timeout: object
    ) -> None:
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text(f"""\
[model]
name = "claude-sonnet-4-6"

[agent]
cli_timeout = {cli_timeout}
""")

        with pytest.raises(ValueError, match="cli_timeout"):
            load_config(cfg_file)


class TestLoadConfigRepositorySection:
    def test_repository_defaults_are_typed(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text(
            """\
[model]
name = "gpt-5.5"

[repository]
owner = "vibesys-playground"
visibility = "internal"
"""
        )

        config = load_config(cfg_file)

        assert config.repository.owner == "vibesys-playground"
        assert config.repository.visibility == "internal"

    @pytest.mark.parametrize("owner", ["owner/name", "spaces are bad", ""])
    def test_invalid_repository_owner_is_rejected(self, tmp_path: Path, owner: object) -> None:
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text(f'[model]\nname = "gpt-5.5"\n\n[repository]\nowner = "{owner}"\n')

        with pytest.raises(ValueError, match="repository owner"):
            load_config(cfg_file)


class TestLoadConfigPresentationBoundary:
    def test_tui_section_is_not_part_of_core_config(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text('[model]\nname = "gpt-5.5"\n\n[tui]\ntheme = "dark"\n')

        with pytest.raises(ValueError, match="tui"):
            load_config(cfg_file)

    def test_application_boundary_can_ignore_tui_section(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text('[model]\nname = "gpt-5.5"\n\n[tui]\ntheme = "dark"\n')

        config = load_config(cfg_file, ignored_sections=frozenset({"tui"}))

        assert config.model.name == "gpt-5.5"

    def test_legacy_outer_role_section_is_rejected(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text("""\
[model]
name = "claude-sonnet-4-6"

[agent.outer]
model = "gpt-legacy"
""")

        with pytest.raises(ValueError, match="outer"):
            load_config(cfg_file)

    def test_core_perf_eval_section_is_rejected(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text(
            '[model]\nname = "claude-sonnet-4-6"\n\n[perf_eval]\nload_levels = []\n'
        )

        with pytest.raises(ValueError, match="perf_eval"):
            load_config(cfg_file)

    def test_invalid_role_id_is_rejected(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text(
            '[model]\nname = "gpt-5.6-sol"\n\n[agent.roles."Bad Role"]\nmodel = "m"\n'
        )

        with pytest.raises(ValueError, match="Bad Role"):
            load_config(cfg_file)

    @pytest.mark.parametrize("field", ["model", "reasoning_effort"])
    @pytest.mark.parametrize("value", ["", "x" * 257])
    def test_invalid_role_policy_text_is_rejected(
        self,
        tmp_path: Path,
        field: str,
        value: str,
    ) -> None:
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text(
            f'[model]\nname = "gpt-5.6-sol"\n\n[agent.roles.orchestrator]\n{field} = "{value}"\n'
        )

        with pytest.raises(ValueError, match=field):
            load_config(cfg_file)

    def test_role_policy_text_accepts_portable_boundary(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "agent.toml"
        value = "x" * 256
        cfg_file.write_text(
            '[model]\nname = "gpt-5.6-sol"\n\n'
            "[agent.roles.orchestrator]\n"
            f'model = "{value}"\n'
            f'reasoning_effort = "{value}"\n'
        )

        config = load_config(cfg_file)

        assert config.agent.roles["orchestrator"].model == value
        assert config.agent.roles["orchestrator"].reasoning_effort == value
