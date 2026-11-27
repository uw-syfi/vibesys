"""Mutable stock candidate entry point. No MLX import until after CLI parsing."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

__all__ = ["PROMPT_CACHE_SIZE", "build_server_command", "main"]

# Bound retained completed-request caches; active batch caches remain native.
PROMPT_CACHE_SIZE = 1


def build_server_command(model: Path, host: str, port: int) -> list[str]:
    """Stock MLX-LM command with one retained cache entry for the 16 GB baseline."""
    return [
        sys.executable,
        "-m",
        "mlx_lm.server",
        "--model",
        str(model),
        "--host",
        host,
        "--port",
        str(port),
        "--temp",
        "0",
        "--max-tokens",
        "64",
        "--prompt-cache-size",
        str(PROMPT_CACHE_SIZE),
    ]


def main() -> None:
    """Resolve pinned local model then replace this process with stock MLX-LM."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", type=Path, default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    configured = args.model_path
    if configured is None and "MLX_MODEL_PATH" in os.environ:
        configured = Path(os.environ["MLX_MODEL_PATH"])
    reference = Path(__file__).resolve().parent / ".vibesys/tasks/shared-prefix/reference/model"
    if configured is None and reference.is_dir():
        configured = reference
    if configured is None:
        parser.error("Pass --model-path or set MLX_MODEL_PATH to the cached pinned snapshot")
    model = configured.expanduser().resolve()
    if not model.is_dir() or not (model / "config.json").is_file():
        parser.error(f"Local model directory is missing or incomplete: {model}")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    command = build_server_command(model, args.host, args.port)
    print(f"Candidate MLX-LM command: {json.dumps(command)}", file=sys.stderr, flush=True)
    os.execv(sys.executable, command)


if __name__ == "__main__":
    main()
