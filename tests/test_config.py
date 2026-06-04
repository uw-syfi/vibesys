import os
import pytest
import tomllib
from pathlib import Path
from unittest.mock import patch

from vibe_serve.config import _load_config, _load_dotenv_file


class TestLoadConfigValid:
    @patch.dict(os.environ, {}, clear=False)
    def test_full_config(self, tmp_path):
        # Clear vertex env vars so they don't override toml values
        os.environ.pop("VERTEX_SERVICE_ACCOUNT_JSON", None)
        os.environ.pop("VERTEX_PROJECT", None)
        os.environ.pop("VERTEX_REGION", None)

        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text("""\
[model]
name = "claude-sonnet-4-6"
provider = "vertex-ai"

[thinking]
level = "medium"
budget = 1024

[providers.vertex-ai]
json = "~/keys/vertex.json"
project = "my-project"
region = "us-east5"

[providers.anthropic]

[providers.google-genai]
""")
        config = _load_config(cfg_file)
        assert config.model.name == "claude-sonnet-4-6"
        assert config.model.provider == "vertex-ai"
        assert config.thinking.level == "medium"
        assert config.thinking.budget == 1024
        assert config.providers.vertex_ai.json_path == "~/keys/vertex.json"
        assert config.providers.vertex_ai.project == "my-project"
        assert config.providers.vertex_ai.region == "us-east5"

    def test_minimal_config(self, tmp_path):
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text('[model]\nname = "claude-sonnet-4-6"\n')
        config = _load_config(cfg_file)
        assert config.model.name == "claude-sonnet-4-6"
        assert config.model.provider is None
        assert config.thinking.level is None
        assert config.thinking.budget is None
        assert config.providers.vertex_ai is None
        assert config.providers.openai_compatible is None


class TestLoadConfigErrors:
    def test_missing_model_name(self, tmp_path):
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text("[model]\nprovider = 'vertex-ai'\n")
        with pytest.raises(ValueError, match="name"):
            _load_config(cfg_file)

    def test_missing_file(self):
        with pytest.raises(FileNotFoundError):
            _load_config(Path("/nonexistent/agent.toml"))

    def test_invalid_toml(self, tmp_path):
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text("not valid toml [[[")
        with pytest.raises(tomllib.TOMLDecodeError):
            _load_config(cfg_file)

    def test_unknown_provider(self, tmp_path):
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text("""\
[model]
name = "claude-sonnet-4-6"
provider = "bedrock"
""")
        with pytest.raises(ValueError, match="bedrock"):
            _load_config(cfg_file)


class TestLoadConfigStrict:
    """Unknown sections/keys are rejected (fail-fast), not silently dropped."""

    def test_unknown_top_level_section_rejected(self, tmp_path):
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text(
            '[model]\nname = "claude-sonnet-4-6"\n\n[bogus]\nx = 1\n'
        )
        with pytest.raises(ValueError, match="bogus"):
            _load_config(cfg_file)

    def test_unknown_key_in_known_section_rejected(self, tmp_path):
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text(
            '[model]\nname = "claude-sonnet-4-6"\n\n[agent]\ncli_modle = "x"\n'
        )
        with pytest.raises(ValueError, match="cli_modle"):
            _load_config(cfg_file)

    def test_unknown_backend_rejected(self, tmp_path):
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text(
            '[model]\nname = "claude-sonnet-4-6"\n\n[backend]\nname = "tpu"\n'
        )
        with pytest.raises(ValueError, match="tpu"):
            _load_config(cfg_file)


class TestLoadConfigProviderDefault:
    def test_missing_provider_defaults_to_none(self, tmp_path):
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text('[model]\nname = "claude-sonnet-4-6"\n')
        config = _load_config(cfg_file)
        assert config.model.provider is None


class TestLoadConfigEnvVars:
    def test_env_var_fallbacks(self, tmp_path):
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text("""\
[model]
name = "claude-sonnet-4-6"
provider = "vertex-ai"

[providers.vertex-ai]
""")
        env = {
            "VERTEX_SERVICE_ACCOUNT_JSON": "/env/key.json",
            "VERTEX_PROJECT": "env-project",
            "VERTEX_REGION": "us-central1",
        }
        with patch.dict("os.environ", env, clear=False):
            config = _load_config(cfg_file)
        vx = config.providers.vertex_ai
        assert vx.json_path == "/env/key.json"
        assert vx.project == "env-project"
        assert vx.region == "us-central1"

    def test_env_vars_override_toml(self, tmp_path):
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text("""\
[model]
name = "claude-sonnet-4-6"
provider = "vertex-ai"

[providers.vertex-ai]
json = "~/keys/vertex.json"
project = "toml-project"
region = "us-east5"
""")
        env = {
            "VERTEX_SERVICE_ACCOUNT_JSON": "/env/key.json",
            "VERTEX_PROJECT": "env-project",
            "VERTEX_REGION": "us-central1",
        }
        with patch.dict("os.environ", env, clear=False):
            config = _load_config(cfg_file)
        vx = config.providers.vertex_ai
        assert vx.json_path == "/env/key.json"
        assert vx.project == "env-project"
        assert vx.region == "us-central1"


class TestLoadDotenvFile:
    def test_load_dotenv_file_parses_values(self, tmp_path):
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

    def test_load_dotenv_file_does_not_override_existing(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("OPENAI_API_KEY=from-file")
        with patch.dict("os.environ", {"OPENAI_API_KEY": "existing"}, clear=True):
            _load_dotenv_file(env_file)
            assert os.environ["OPENAI_API_KEY"] == "existing"


class TestLoadConfigThinking:
    def test_thinking_parsed(self, tmp_path):
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text("""\
[model]
name = "gemini-2.5-pro"

[thinking]
level = "high"
budget = 2048
""")
        config = _load_config(cfg_file)
        assert config.thinking.level == "high"
        assert config.thinking.budget == 2048


class TestLoadConfigAgentSection:
    def test_agent_section_preserved(self, tmp_path):
        # The [agent] table drives build_agent_runner (cli_model, cli_timeout,
        # backend, cli_provider). _load_config must carry it through; the
        # previous allowlist loader silently dropped it.
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text("""\
[model]
name = "claude-sonnet-4-6"

[agent]
backend = "cli"
cli_provider = "claude"
cli_model = "claude-opus-4-8[1m]"
cli_timeout = 1800
""")
        config = _load_config(cfg_file)
        assert config.agent.cli_model == "claude-opus-4-8[1m]"
        assert config.agent.cli_timeout == 1800
        assert config.agent.backend == "cli"
        assert config.agent.cli_provider == "claude"

    def test_agent_section_defaults_to_empty(self, tmp_path):
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text('[model]\nname = "claude-sonnet-4-6"\n')
        config = _load_config(cfg_file)
        assert config.agent.backend is None
        assert config.agent.cli_provider is None
        assert config.agent.cli_model is None
        assert config.agent.cli_timeout is None


class TestLoadConfigPerfEval:
    def test_load_levels_preserved(self, tmp_path):
        # [perf_eval].load_levels feeds the perf_eval prompt template; the
        # allowlist loader dropped this section entirely.
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text("""\
[model]
name = "claude-sonnet-4-6"

[[perf_eval.load_levels]]
rate = 1
duration = 20
max_tokens = 128

[[perf_eval.load_levels]]
rate = 8
duration = 20
max_tokens = 256
""")
        config = _load_config(cfg_file)
        levels = config.perf_eval.load_levels
        assert levels is not None
        assert [lvl.rate for lvl in levels] == [1, 8]
        assert levels[1].max_tokens == 256

    def test_perf_eval_defaults_to_none(self, tmp_path):
        cfg_file = tmp_path / "agent.toml"
        cfg_file.write_text('[model]\nname = "claude-sonnet-4-6"\n')
        config = _load_config(cfg_file)
        assert config.perf_eval.load_levels is None
