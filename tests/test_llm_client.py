import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from vibe_serve.config import Config
from vibe_serve.llm_client import _build_model


def _make_config(
    name="claude-sonnet-4-6",
    provider=None,
    thinking=None,
    providers=None,
):
    """Helper to build a validated :class:`Config` matching _load_config output."""
    return Config.model_validate(
        {
            "model": {"name": name, "provider": provider},
            "thinking": thinking or {},
            "providers": providers or {},
        }
    )


FAKE_CREDS_DICT = {
    "type": "service_account",
    "project_id": "test-project",
    "private_key_id": "key123",
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----\n",
    "client_email": "test@test-project.iam.gserviceaccount.com",
    "client_id": "123",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
}


@pytest.fixture()
def key_file(tmp_path):
    kf = tmp_path / "key.json"
    kf.write_text(json.dumps(FAKE_CREDS_DICT))
    return kf


# --- Auto-detect provider (provider=None) ---


class TestAutoDetectProvider:
    def test_claude_model_returns_anthropic_string(self):
        config = _make_config("claude-sonnet-4-6")
        assert _build_model(config) == "anthropic:claude-sonnet-4-6"

    def test_gemini_model_returns_google_genai_string(self):
        config = _make_config("gemini-2.5-pro")
        assert _build_model(config) == "google_genai:gemini-2.5-pro"

    def test_unknown_model_prefix_raises(self):
        config = _make_config("llama-3-70b")
        with pytest.raises(ValueError, match="Cannot auto-detect"):
            _build_model(config)


# --- Direct Anthropic provider ---


class TestAnthropicProvider:
    def test_anthropic_claude_model(self):
        config = _make_config("claude-sonnet-4-6", provider="anthropic")
        assert _build_model(config) == "anthropic:claude-sonnet-4-6"

    def test_anthropic_non_claude_model_raises(self):
        config = _make_config("gemini-2.5-pro", provider="anthropic")
        with pytest.raises(ValueError, match="not a Claude model"):
            _build_model(config)


# --- Direct Google GenAI provider ---


class TestGoogleGenaiProvider:
    def test_google_genai_gemini_model(self):
        config = _make_config("gemini-2.5-pro", provider="google-genai")
        assert _build_model(config) == "google_genai:gemini-2.5-pro"

    def test_google_genai_non_google_model_raises(self):
        config = _make_config("claude-sonnet-4-6", provider="google-genai")
        with pytest.raises(ValueError, match="not a Google model"):
            _build_model(config)


# --- Unknown provider ---


class TestUnknownProvider:
    def test_unknown_provider_rejected_by_schema(self):
        # Provider validation now happens at the Config boundary (fail-fast),
        # not inside _build_model.
        with pytest.raises(ValueError, match="bedrock"):
            _make_config("claude-sonnet-4-6", provider="bedrock")


# --- Vertex AI provider ---


class TestVertexAIProvider:
    @patch("google.oauth2.service_account.Credentials.from_service_account_info")
    @patch("langchain_google_vertexai.model_garden.ChatAnthropicVertex")
    def test_vertex_claude_model(self, mock_chat_cls, mock_from_sa, key_file):
        mock_creds = MagicMock()
        mock_from_sa.return_value = mock_creds
        config = _make_config(
            "claude-sonnet-4-6",
            provider="vertex-ai",
            providers={"vertex-ai": {"json": str(key_file), "project": None, "region": "us-east5"}},
        )
        _build_model(config)
        mock_chat_cls.assert_called_once_with(
            model_name="claude-sonnet-4-6",
            credentials=mock_creds,
            project="test-project",
            location="us-east5",
        )

    @patch("google.oauth2.service_account.Credentials.from_service_account_info")
    @patch("langchain_google_genai.ChatGoogleGenerativeAI")
    def test_vertex_gemini_model(self, mock_chat_cls, mock_from_sa, key_file):
        mock_creds = MagicMock()
        mock_from_sa.return_value = mock_creds
        config = _make_config(
            "gemini-2.5-pro",
            provider="vertex-ai",
            providers={"vertex-ai": {"json": str(key_file), "project": None, "region": "us-east5"}},
        )
        _build_model(config)
        mock_chat_cls.assert_called_once_with(
            model="gemini-2.5-pro",
            credentials=mock_creds,
            project="test-project",
            location="us-east5",
        )

    @patch("google.oauth2.service_account.Credentials.from_service_account_info")
    @patch("langchain_google_genai.ChatGoogleGenerativeAI")
    def test_vertex_gemini_thinking_level(self, mock_chat_cls, mock_from_sa, key_file):
        mock_creds = MagicMock()
        mock_from_sa.return_value = mock_creds
        config = _make_config(
            "gemini-2.5-pro",
            provider="vertex-ai",
            thinking={"level": "medium"},
            providers={"vertex-ai": {"json": str(key_file), "project": None, "region": "us-east5"}},
        )
        _build_model(config)
        mock_chat_cls.assert_called_once_with(
            model="gemini-2.5-pro",
            credentials=mock_creds,
            project="test-project",
            location="us-east5",
            thinking_level="medium",
            include_thoughts=True,
        )

    @patch("google.oauth2.service_account.Credentials.from_service_account_info")
    @patch("langchain_google_genai.ChatGoogleGenerativeAI")
    def test_vertex_gemini_thinking_budget(self, mock_chat_cls, mock_from_sa, key_file):
        mock_creds = MagicMock()
        mock_from_sa.return_value = mock_creds
        config = _make_config(
            "gemini-2.5-pro",
            provider="vertex-ai",
            thinking={"budget": 1024},
            providers={"vertex-ai": {"json": str(key_file), "project": None, "region": "us-east5"}},
        )
        _build_model(config)
        mock_chat_cls.assert_called_once_with(
            model="gemini-2.5-pro",
            credentials=mock_creds,
            project="test-project",
            location="us-east5",
            thinking_budget=1024,
            include_thoughts=True,
        )

    @patch("google.oauth2.service_account.Credentials.from_service_account_info")
    @patch("langchain_google_genai.ChatGoogleGenerativeAI")
    def test_vertex_gemini_thinking_level_takes_precedence(self, mock_chat_cls, mock_from_sa, key_file):
        mock_creds = MagicMock()
        mock_from_sa.return_value = mock_creds
        config = _make_config(
            "gemini-2.5-pro",
            provider="vertex-ai",
            thinking={"level": "high", "budget": 1024},
            providers={"vertex-ai": {"json": str(key_file), "project": None, "region": "us-east5"}},
        )
        _build_model(config)
        call_kwargs = mock_chat_cls.call_args[1]
        assert call_kwargs["thinking_level"] == "high"
        assert call_kwargs["include_thoughts"] is True
        assert "thinking_budget" not in call_kwargs

    def test_vertex_missing_json_key_raises(self):
        config = _make_config(
            "claude-sonnet-4-6",
            provider="vertex-ai",
            providers={"vertex-ai": {"json": "/nonexistent/key.json", "project": None, "region": "us-east5"}},
        )
        with pytest.raises(ValueError, match="service account key not found"):
            _build_model(config)

    @patch("google.oauth2.service_account.Credentials.from_service_account_info")
    @patch("langchain_google_vertexai.model_garden.ChatAnthropicVertex")
    def test_vertex_project_overrides_json(self, mock_chat_cls, mock_from_sa, key_file):
        mock_creds = MagicMock()
        mock_from_sa.return_value = mock_creds
        config = _make_config(
            "claude-sonnet-4-6",
            provider="vertex-ai",
            providers={"vertex-ai": {"json": str(key_file), "project": "override-project", "region": "us-east5"}},
        )
        _build_model(config)
        mock_chat_cls.assert_called_once_with(
            model_name="claude-sonnet-4-6",
            credentials=mock_creds,
            project="override-project",
            location="us-east5",
        )


# --- Integration: config → model ---


class TestConfigToModel:
    def test_full_pipeline_anthropic(self):
        config = _make_config("claude-sonnet-4-6", provider="anthropic")
        assert _build_model(config) == "anthropic:claude-sonnet-4-6"

    def test_full_pipeline_google_genai(self):
        config = _make_config("gemini-2.5-pro", provider="google-genai")
        assert _build_model(config) == "google_genai:gemini-2.5-pro"

    def test_full_pipeline_openai(self):
        config = _make_config("gpt-4o", provider="openai")
        assert _build_model(config) == "openai:gpt-4o"


# --- OpenAI provider ---


class TestOpenAIProvider:
    def test_openai_gpt_model(self):
        config = _make_config("gpt-4o", provider="openai")
        assert _build_model(config) == "openai:gpt-4o"

    def test_openai_o1_model(self):
        config = _make_config("o1", provider="openai")
        assert _build_model(config) == "openai:o1"

    def test_openai_o3_model(self):
        config = _make_config("o3-mini", provider="openai")
        assert _build_model(config) == "openai:o3-mini"

    def test_openai_o4_model(self):
        config = _make_config("o4-mini", provider="openai")
        assert _build_model(config) == "openai:o4-mini"

    def test_openai_non_openai_model_raises(self):
        config = _make_config("claude-sonnet-4-6", provider="openai")
        with pytest.raises(ValueError, match="not an OpenAI model"):
            _build_model(config)

    def test_auto_detect_openai_gpt(self):
        config = _make_config("gpt-4o")
        assert _build_model(config) == "openai:gpt-4o"

    def test_auto_detect_openai_o1(self):
        config = _make_config("o1")
        assert _build_model(config) == "openai:o1"

    def test_auto_detect_openai_o3(self):
        config = _make_config("o3-mini")
        assert _build_model(config) == "openai:o3-mini"


# --- Thinking not supported ---


class TestThinkingNotSupported:
    def test_anthropic_with_thinking_raises(self):
        config = _make_config("claude-sonnet-4-6", provider="anthropic", thinking={"level": "medium"})
        with pytest.raises(ValueError, match="[Tt]hinking.*not supported"):
            _build_model(config)

    def test_openai_with_thinking_raises(self):
        config = _make_config("gpt-4o", provider="openai", thinking={"level": "high"})
        with pytest.raises(ValueError, match="[Tt]hinking.*not supported"):
            _build_model(config)

    def test_google_genai_with_thinking_raises(self):
        config = _make_config("gemini-2.5-pro", provider="google-genai", thinking={"budget": 1024})
        with pytest.raises(ValueError, match="[Tt]hinking.*not supported"):
            _build_model(config)

    def test_auto_detect_anthropic_with_thinking_raises(self):
        config = _make_config("claude-sonnet-4-6", thinking={"level": "low"})
        with pytest.raises(ValueError, match="[Tt]hinking.*not supported"):
            _build_model(config)

    def test_auto_detect_openai_with_thinking_raises(self):
        config = _make_config("gpt-4o", thinking={"budget": 512})
        with pytest.raises(ValueError, match="[Tt]hinking.*not supported"):
            _build_model(config)

    def test_auto_detect_google_genai_with_thinking_raises(self):
        config = _make_config("gemini-2.5-pro", thinking={"level": "medium"})
        with pytest.raises(ValueError, match="[Tt]hinking.*not supported"):
            _build_model(config)

    @patch("google.oauth2.service_account.Credentials.from_service_account_info")
    @patch("langchain_google_vertexai.model_garden.ChatAnthropicVertex")
    def test_vertex_claude_with_thinking_raises(self, mock_chat_cls, mock_from_sa, key_file):
        mock_from_sa.return_value = MagicMock()
        config = _make_config(
            "claude-sonnet-4-6",
            provider="vertex-ai",
            thinking={"level": "medium"},
            providers={"vertex-ai": {"json": str(key_file), "project": None, "region": "us-east5"}},
        )
        with pytest.raises(ValueError, match="[Tt]hinking.*not supported"):
            _build_model(config)
