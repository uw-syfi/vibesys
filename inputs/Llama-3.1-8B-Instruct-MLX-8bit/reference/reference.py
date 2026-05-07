"""Reference inference for the MLX 8-bit Llama 3.1 8B Instruct target.

This bundle is the MLX 8-bit quantized target model used by the speculative
decoding playground. Implementations should match this reference for greedy
decoding when no structured-output constraints are active.

Install:
    uv pip install mlx mlx-lm huggingface_hub

Run:
    python reference.py --prompt "The capital of France is" --max-tokens 16
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


REFERENCE_DIR = Path(__file__).resolve().parent
DEFAULT_MODEL_DIR = REFERENCE_DIR / "model"


def _repo_root() -> Path:
    return REFERENCE_DIR.parent.parent.parent


def _read_meta() -> dict:
    return json.loads((REFERENCE_DIR / "meta.json").read_text())


def ensure_model_dir(model_dir: str | Path | None = None) -> Path:
    """Return a local MLX model snapshot directory, downloading if needed."""
    if model_dir is not None:
        candidate = Path(model_dir).expanduser().resolve()
        if candidate.exists():
            return candidate
        if candidate != DEFAULT_MODEL_DIR.resolve():
            raise FileNotFoundError(f"Model directory does not exist: {candidate}")

    if DEFAULT_MODEL_DIR.exists():
        return DEFAULT_MODEL_DIR.resolve()

    meta = _read_meta()
    from huggingface_hub import snapshot_download

    downloaded = Path(
        snapshot_download(
            meta["model_id"],
            revision=meta.get("revision"),
            cache_dir=str(_repo_root() / ".hf_cache"),
        )
    )
    try:
        DEFAULT_MODEL_DIR.symlink_to(downloaded, target_is_directory=True)
        return DEFAULT_MODEL_DIR.resolve()
    except FileExistsError:
        return DEFAULT_MODEL_DIR.resolve()
    except OSError:
        return downloaded


def reference(
    prompt: str,
    max_tokens: int = 64,
    model_dir: str | Path | None = DEFAULT_MODEL_DIR,
) -> str:
    """Greedy-decode `prompt` with the pinned MLX 8-bit target model."""
    from mlx_lm import generate, load

    resolved_model_dir = ensure_model_dir(model_dir)
    model, tokenizer = load(str(resolved_model_dir))
    return generate(
        model,
        tokenizer,
        prompt=prompt,
        max_tokens=max_tokens,
        temp=0.0,
        verbose=False,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", default="The capital of France is")
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    print(reference(args.prompt, args.max_tokens, args.model_dir))
