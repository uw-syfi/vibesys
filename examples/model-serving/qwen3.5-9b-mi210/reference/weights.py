"""Load the text-model weights of a Qwen3.5 checkpoint into `Qwen35ForCausalLM`."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import torch
from safetensors import safe_open

from .config import TextConfig
from .model import Qwen35ForCausalLM

log = logging.getLogger(__name__)

_TEXT_PREFIX = "model.language_model."
_LM_HEAD = "lm_head.weight"


def _checkpoint_name_to_param(name: str) -> str | None:
    """Map a checkpoint tensor name to a model parameter name, or None to skip it (vision, MTP)."""
    if name == _LM_HEAD:
        return name
    if name.startswith(_TEXT_PREFIX):
        return name[len(_TEXT_PREFIX) :]
    return None


def load_model(
    model_dir: Path, cfg: TextConfig, device: torch.device, dtype: torch.dtype
) -> Qwen35ForCausalLM:
    """Build the model on `meta`, then materialize every parameter straight from safetensors.

    All floating tensors are cast to `dtype` (bf16), matching HF
    `from_pretrained(dtype=torch.bfloat16)`. Loading is strict: a missing or
    unexpected text tensor is an error.
    """
    with torch.device("meta"):
        model = Qwen35ForCausalLM(cfg)
    index = json.loads((model_dir / "model.safetensors.index.json").read_text())
    files = sorted(set(index["weight_map"].values()))
    state: dict[str, torch.Tensor] = {}
    cast: list[str] = []
    for fname in files:
        with safe_open(str(model_dir / fname), framework="pt", device=str(device)) as f:
            for name in f.keys():
                param = _checkpoint_name_to_param(name)
                if param is None:
                    continue
                tensor = f.get_tensor(name)
                if tensor.dtype != dtype:
                    cast.append(f"{param}:{tensor.dtype}")
                state[param] = tensor.to(dtype)
    if cast:  # the checkpoint stores GDN A_log and norm.weight in fp32; HF casts them too
        log.info("cast %d tensors to %s (e.g. %s)", len(cast), dtype, ", ".join(cast[:2]))
    # Non-persistent buffers (rotary inv_freq) are rebuilt on the device, in fp32.
    missing, unexpected = model.load_state_dict(state, strict=False, assign=True)
    if unexpected or missing:
        raise ValueError(
            f"checkpoint mismatch: missing={sorted(missing)[:8]} unexpected={sorted(unexpected)[:8]}"
        )
    model.rotary.inv_freq = type(model.rotary)(cfg).inv_freq.to(device)
    return model.eval()
