import os
import tomllib
from pathlib import Path

from vibe_serve.constants import ComputeBackend, DEFAULT_COMPUTE_BACKEND, PROJECT_ROOT

_KNOWN_PROVIDERS = {"vertex-ai", "anthropic", "google-genai", "openai", "openai-compatible"}


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


def _load_config(path: Path) -> dict:
    """Load and validate a TOML agent config file."""
    _load_dotenv_file()
    path = Path(path)
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    model = raw.get("model", {})
    if "name" not in model:
        raise ValueError("Config missing required field: model.name")

    provider = model.get("provider")
    if provider is not None and provider not in _KNOWN_PROVIDERS:
        raise ValueError(
            f"Unknown provider {provider!r} in config. "
            f"Supported providers: {', '.join(sorted(_KNOWN_PROVIDERS))}"
        )

    thinking = raw.get("thinking", {})
    providers = raw.get("providers", {})

    backend_section = raw.get("backend", {}) or {}
    raw_backend = backend_section.get("name", DEFAULT_COMPUTE_BACKEND)
    try:
        backend = ComputeBackend(raw_backend)
    except ValueError:
        raise ValueError(
            f"Unknown backend {raw_backend!r} in config. "
            f"Supported backends: {', '.join(b.value for b in ComputeBackend)}"
        ) from None

    # Apply env var overrides for vertex-ai
    if "vertex-ai" in providers:
        vx = providers["vertex-ai"]
        env_json = os.environ.get("VERTEX_SERVICE_ACCOUNT_JSON")
        env_project = os.environ.get("VERTEX_PROJECT")
        env_region = os.environ.get("VERTEX_REGION")
        if env_json:
            vx["json"] = env_json
        if env_project:
            vx["project"] = env_project
        if env_region:
            vx["region"] = env_region

    # Normalize provider to None if not set
    config = {
        "model": {"name": model["name"], "provider": provider},
        "thinking": thinking,
        "providers": providers,
        "backend": {"name": backend},
        # Preserve the [agent] section so its settings (cli_model, cli_timeout,
        # backend, cli_provider) actually reach build_agent_runner. Without this
        # the table is parsed and silently discarded.
        "agent": raw.get("agent", {}) or {},
    }
    # Preserve any non-`name` fields the user added to [backend] for forward
    # compatibility (future backends may carry their own sub-config).
    for key, value in backend_section.items():
        if key != "name":
            config["backend"][key] = value
    return config
