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
    name: str
    # When None the provider is auto-detected from the model name prefix.
    provider: Provider | None = None


class ThinkingCfg(_Strict):
    level: str | None = None
    budget: int | None = None


class VertexCfg(_Strict):
    # TOML key is ``json`` (path to the service-account key file); the attribute
    # is renamed to avoid shadowing ``BaseModel.json``.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    json_path: str | None = Field(default=None, alias="json")
    project: str | None = None
    region: str = "us-east5"


class OpenAICompatCfg(_Strict):
    base_url: str | None = None
    api_key: str = "no-key"


class _CredEnvProviderCfg(_Strict):
    """A provider whose credentials come from the environment (``.env``).

    The ``[providers.<name>]`` table carries no keys; it exists only as a marker.
    Declared so the table validates under ``extra="forbid"`` while still
    rejecting stray keys placed under it.
    """


class ProvidersCfg(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    vertex_ai: VertexCfg | None = Field(default=None, alias="vertex-ai")
    openai_compatible: OpenAICompatCfg | None = Field(
        default=None, alias="openai-compatible"
    )
    anthropic: _CredEnvProviderCfg | None = None
    google_genai: _CredEnvProviderCfg | None = Field(default=None, alias="google-genai")
    openai: _CredEnvProviderCfg | None = None


class BackendCfg(_Strict):
    # Coerced from the TOML string (e.g. "cuda") into the enum.
    name: ComputeBackend = DEFAULT_COMPUTE_BACKEND


class AgentCfg(_Strict):
    backend: str | None = None
    cli_provider: str | None = None
    cli_model: str | None = None
    cli_timeout: int | None = None


class LoadLevelCfg(_Strict):
    """One benchmark load level fed to the perf_eval prompt template.

    Distinct from the ``LoadLevelMetrics`` *output* schema in ``schemas.py``.
    """

    rate: int
    duration: int
    max_tokens: int


class PerfEvalCfg(_Strict):
    load_levels: list[LoadLevelCfg] | None = None


class Config(_Strict):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model: ModelCfg
    thinking: ThinkingCfg = Field(default_factory=ThinkingCfg)
    providers: ProvidersCfg = Field(default_factory=ProvidersCfg)
    backend: BackendCfg = Field(default_factory=BackendCfg)
    agent: AgentCfg = Field(default_factory=AgentCfg)
    perf_eval: PerfEvalCfg = Field(default_factory=PerfEvalCfg)


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
