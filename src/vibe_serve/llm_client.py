import json
from pathlib import Path

from vibe_serve.config import Config, ThinkingCfg
from vibe_serve.constants import _ANTHROPIC_PREFIXES, _GOOGLE_PREFIXES, _OPENAI_PREFIXES


def _is_google_model(model_name: str) -> bool:
    return any(model_name.startswith(p) for p in _GOOGLE_PREFIXES)


def _is_anthropic_model(model_name: str) -> bool:
    return any(model_name.startswith(p) for p in _ANTHROPIC_PREFIXES)


def _is_openai_model(model_name: str) -> bool:
    return any(model_name.startswith(p) for p in _OPENAI_PREFIXES)


def _has_thinking(thinking: ThinkingCfg) -> bool:
    return bool(thinking.level or thinking.budget)


def _build_model(config: Config):
    """Build the chat model from a parsed :class:`Config`."""
    model_name = config.model.name
    provider = config.model.provider
    thinking = config.thinking

    if provider == "vertex-ai":
        return _build_vertex_model(model_name, config, thinking)

    if provider == "anthropic":
        if not _is_anthropic_model(model_name):
            raise ValueError(f"{model_name!r} is not a Claude model (provider='anthropic')")
        if _has_thinking(thinking):
            raise ValueError("Thinking is not supported for provider 'anthropic'")
        return f"anthropic:{model_name}"

    if provider == "google-genai":
        if not _is_google_model(model_name):
            raise ValueError(f"{model_name!r} is not a Google model (provider='google-genai')")
        if _has_thinking(thinking):
            raise ValueError("Thinking is not supported for provider 'google-genai'")
        return f"google_genai:{model_name}"

    if provider == "openai":
        if not _is_openai_model(model_name):
            raise ValueError(f"{model_name!r} is not an OpenAI model (provider='openai')")
        if _has_thinking(thinking):
            raise ValueError("Thinking is not supported for provider 'openai'")
        return f"openai:{model_name}"

    if provider == "openai-compatible":
        return _build_openai_compatible_model(model_name, config)

    if provider is None:
        # Auto-detect from model name
        if _is_anthropic_model(model_name):
            if _has_thinking(thinking):
                raise ValueError("Thinking is not supported for provider 'anthropic'")
            return f"anthropic:{model_name}"
        if _is_google_model(model_name):
            if _has_thinking(thinking):
                raise ValueError("Thinking is not supported for provider 'google-genai'")
            return f"google_genai:{model_name}"
        if _is_openai_model(model_name):
            if _has_thinking(thinking):
                raise ValueError("Thinking is not supported for provider 'openai'")
            return f"openai:{model_name}"
        raise ValueError(
            f"Cannot auto-detect provider for model {model_name!r}. "
            f"Set model.provider in your config."
        )

    raise NotImplementedError(f"Provider {provider!r} is not yet supported")


def _build_openai_compatible_model(model_name: str, config: Config):
    """Build a model using an OpenAI-compatible API (e.g. vLLM, Ollama)."""
    from langchain_openai import ChatOpenAI

    oc = config.providers.openai_compatible
    base_url = oc.base_url if oc else None
    if not base_url:
        raise ValueError(
            "openai-compatible provider requires 'base_url' "
            "(e.g. 'http://localhost:8000/v1')"
        )
    api_key = oc.api_key

    return ChatOpenAI(
        model=model_name,
        base_url=base_url,
        api_key=api_key,
    )


def _build_vertex_model(model_name: str, config: Config, thinking: ThinkingCfg):
    """Build a Vertex AI model (Claude via Model Garden or Gemini via GenAI)."""
    from google.oauth2 import service_account

    vx = config.providers.vertex_ai
    vertex_json = vx.json_path if vx else None
    vertex_project = vx.project if vx else None
    vertex_region = vx.region if vx else "us-east5"

    if not vertex_json:
        raise ValueError("vertex-ai provider requires 'json' key path")

    key_path = Path(vertex_json).expanduser()
    if not key_path.exists():
        raise ValueError(f"Vertex AI service account key not found: {key_path}")

    creds_dict = json.loads(key_path.read_text())
    credentials = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    project = vertex_project or creds_dict.get("project_id")

    if not _is_google_model(model_name) and _has_thinking(thinking):
        raise ValueError("Thinking is not supported for non-Gemini models on Vertex AI")

    if _is_google_model(model_name):
        from langchain_google_genai import ChatGoogleGenerativeAI

        thinking_kwargs = {}
        thinking_level = thinking.level
        thinking_budget = thinking.budget
        if thinking_level is not None:
            thinking_kwargs["thinking_level"] = thinking_level
            thinking_kwargs["include_thoughts"] = True
        elif thinking_budget is not None:
            thinking_kwargs["thinking_budget"] = thinking_budget
            thinking_kwargs["include_thoughts"] = True

        return ChatGoogleGenerativeAI(
            model=model_name,
            credentials=credentials,
            project=project,
            location=vertex_region,
            **thinking_kwargs,
        )
    else:
        from langchain_google_vertexai.model_garden import ChatAnthropicVertex

        return ChatAnthropicVertex(
            model_name=model_name,
            credentials=credentials,
            project=project,
            location=vertex_region,
        )
