"""Typed, schema-driven configuration for ``agent.toml``.

The whole config is described by the :class:`Config` pydantic model and its
nested sections. ``_load_config`` parses the TOML, validates it against that
schema (fail-fast: missing required fields, unknown providers/backends, wrong
types, **and unknown keys** all raise), applies environment-variable overrides,
and returns a typed :class:`Config`. Consumers use attribute access
(``config.model.name``) — there is no dict-style access anywhere.

Every section is ``extra="forbid"``: a stray or misspelled key is an error
rather than being silently dropped, which is the failure mode the previous
allowlist loader suffered from.
"""

import os
import tomllib
from pathlib import Path
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from vibe_serve.constants import ComputeBackend, DEFAULT_COMPUTE_BACKEND, PROJECT_ROOT

Provider = Literal[
    "vertex-ai", "anthropic", "google-genai", "openai", "openai-compatible"
]


class _Strict(BaseModel):
    """Base for every config section: reject unknown keys."""

    model_config = ConfigDict(extra="forbid")


class ModelCfg(_Strict):
    name: str = Field(
        description="Model identifier, e.g. 'claude-sonnet-4-6'. Required."
    )
    provider: Provider | None = Field(
        default=None,
        description=(
            "Provider override. When omitted, auto-detected from the model-name "
            "prefix: claude-* → anthropic, gpt-*/o1/o3/o4 → openai, "
            "gemini-*/gemma-* → google-genai."
        ),
    )


class ThinkingCfg(_Strict):
    level: str | None = Field(
        default=None,
        description=(
            "Reasoning effort level passed to the model (provider-specific, e.g. "
            "'low'/'medium'/'high'). For Gemini on Vertex, mutually exclusive with "
            "budget."
        ),
    )
    budget: int | None = Field(
        default=None,
        description="Thinking token budget (provider-specific). Alternative to level.",
    )


class VertexCfg(_Strict):
    # The attribute is ``json_path`` to avoid shadowing ``BaseModel.json``; the
    # TOML key stays ``json`` via the alias.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    json_path: str | None = Field(
        default=None,
        alias="json",
        description=(
            "Path to the Vertex AI service-account JSON key file. Overridable via "
            "$VERTEX_SERVICE_ACCOUNT_JSON."
        ),
    )
    project: str | None = Field(
        default=None,
        description=(
            "GCP project id. Falls back to the key file's project_id when unset. "
            "Overridable via $VERTEX_PROJECT."
        ),
    )
    region: str = Field(
        default="us-east5",
        description="Vertex AI region/location. Overridable via $VERTEX_REGION.",
    )


class OpenAICompatCfg(_Strict):
    base_url: str | None = Field(
        default=None,
        description=(
            "Base URL of the OpenAI-compatible endpoint "
            "(e.g. 'http://localhost:8000/v1'). Required for this provider."
        ),
    )
    api_key: str = Field(
        default="no-key",
        description="API key for the endpoint; 'no-key' for unauthenticated local servers.",
    )


class _CredEnvProviderCfg(_Strict):
    """A provider whose credentials come from the environment (``.env``).

    The ``[providers.<name>]`` table carries no keys; it exists only as a marker.
    Declared so the table validates under ``extra="forbid"`` while still
    rejecting stray keys placed under it.
    """


class ProvidersCfg(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    vertex_ai: VertexCfg | None = Field(
        default=None,
        alias="vertex-ai",
        description="Vertex AI provider settings ([providers.vertex-ai]).",
    )
    openai_compatible: OpenAICompatCfg | None = Field(
        default=None,
        alias="openai-compatible",
        description=(
            "OpenAI-compatible endpoint settings ([providers.openai-compatible])."
        ),
    )
    anthropic: _CredEnvProviderCfg | None = Field(
        default=None,
        description="Anthropic provider marker; credentials from $ANTHROPIC_API_KEY.",
    )
    google_genai: _CredEnvProviderCfg | None = Field(
        default=None,
        alias="google-genai",
        description="Google GenAI provider marker; credentials from $GOOGLE_API_KEY.",
    )
    openai: _CredEnvProviderCfg | None = Field(
        default=None,
        description="OpenAI provider marker; credentials from $OPENAI_API_KEY.",
    )


class BackendCfg(_Strict):
    name: ComputeBackend = Field(
        default=DEFAULT_COMPUTE_BACKEND,
        description="Compute backend, coerced from the TOML string. One of: cuda, metal.",
    )


class AgentCfg(_Strict):
    backend: str | None = Field(
        default=None,
        description=(
            "Agent runner backend: 'cli' (drive an external coding-agent CLI) or "
            "'deepagents'. The --agent-backend flag overrides; defaults to 'cli'."
        ),
    )
    cli_provider: str | None = Field(
        default=None,
        description=(
            "Which CLI coding-agent to drive: codex | claude | gemini | opencode. "
            "The --cli-provider flag overrides; defaults to 'codex'."
        ),
    )
    cli_model: str | None = Field(
        default=None,
        description=(
            "Model the CLI tool should use; overrides model.name for the CLI agent. "
            "None → the CLI tool's own default (no --model flag passed)."
        ),
    )
    cli_timeout: int | None = Field(
        default=None,
        description=(
            "Per-invocation timeout for the CLI agent, in seconds. None → the "
            "runner default."
        ),
    )


class LoadLevelCfg(_Strict):
    """One benchmark load level fed to the perf_eval prompt template.

    Distinct from the ``LoadLevelMetrics`` *output* schema in ``schemas.py``.
    """

    rate: int = Field(description="Request rate (requests/sec) for this load level.")
    duration: int = Field(description="Benchmark duration in seconds at this load level.")
    max_tokens: int = Field(
        description="Max output tokens per request at this load level."
    )


class PerfEvalCfg(_Strict):
    load_levels: list[LoadLevelCfg] | None = Field(
        default=None,
        description=(
            "Benchmark load levels handed to the perf evaluator. None → the "
            "evaluator uses its built-in default ladder."
        ),
    )


class Config(_Strict):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model: ModelCfg = Field(
        description="[model] — model name and provider. Required."
    )
    thinking: ThinkingCfg = Field(
        default_factory=ThinkingCfg, description="[thinking] — reasoning/thinking controls."
    )
    providers: ProvidersCfg = Field(
        default_factory=ProvidersCfg,
        description="[providers.*] — per-provider credentials and endpoints.",
    )
    backend: BackendCfg = Field(
        default_factory=BackendCfg, description="[backend] — compute backend selection."
    )
    agent: AgentCfg = Field(
        default_factory=AgentCfg,
        description="[agent] — agent runner backend and CLI-agent settings.",
    )
    perf_eval: PerfEvalCfg = Field(
        default_factory=PerfEvalCfg,
        description="[perf_eval] — performance-evaluation settings.",
    )


def as_config(config: "Config | Mapping") -> "Config":
    """Coerce a mapping to a validated :class:`Config`; pass through instances.

    The loop entrypoints accept either a parsed :class:`Config` (the normal CLI
    path) or a raw mapping (tests, programmatic callers) and normalize here.
    """
    return config if isinstance(config, Config) else Config.model_validate(config)


def _load_dotenv_file(path: Path = PROJECT_ROOT / ".env") -> None:
    """Load environment variables from a .env-style file.

    Supports KEY=VALUE lines with optional quotes. Existing environment variables
    are preserved and not overwritten.
    """
    if not path.exists():
        return

    with path.open(encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            if line.startswith("export "):
                line = line[len("export ") :].strip()

            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                continue

            value = value.strip()
            if not value:
                os.environ.setdefault(key, "")
                continue
            if (
                (value[0] == value[-1] == '"')
                or (value[0] == value[-1] == "'")
            ):
                value = value[1:-1]
            os.environ.setdefault(key, value)


def _apply_vertex_env_overrides(config: Config) -> None:
    """Let ``VERTEX_*`` env vars override the ``[providers.vertex-ai]`` table.

    Only applied when the section is present, matching prior behavior.
    """
    vx = config.providers.vertex_ai
    if vx is None:
        return
    if env_json := os.environ.get("VERTEX_SERVICE_ACCOUNT_JSON"):
        vx.json_path = env_json
    if env_project := os.environ.get("VERTEX_PROJECT"):
        vx.project = env_project
    if env_region := os.environ.get("VERTEX_REGION"):
        vx.region = env_region


def _load_config(path: Path) -> Config:
    """Load, validate, and env-override a TOML agent config file."""
    _load_dotenv_file()
    path = Path(path)
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    config = Config.model_validate(raw)
    _apply_vertex_env_overrides(config)
    return config
