"""Typed text-model config for Qwen3.5 dense checkpoints (`model_type: qwen3_5`).
The checkpoint is a VLM wrapper: the language model lives under `text_config`.
Only the text path is served; the vision tower and the MTP head are ignored.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from huggingface_hub import snapshot_download


class LayerType(StrEnum):
    LINEAR = "linear_attention"  # Gated DeltaNet
    FULL = "full_attention"  # gated GQA softmax attention


@dataclass(frozen=True)
class TextConfig:
    vocab_size: int
    hidden_size: int
    intermediate_size: int
    num_hidden_layers: int
    layer_types: tuple[LayerType, ...]
    rms_norm_eps: float
    # Full attention.
    num_attention_heads: int
    num_key_value_heads: int
    head_dim: int
    rope_theta: float
    partial_rotary_factor: float
    # Gated DeltaNet.
    linear_num_key_heads: int
    linear_num_value_heads: int
    linear_key_head_dim: int
    linear_value_head_dim: int
    linear_conv_kernel_dim: int
    eos_token_id: int

    @property
    def rotary_dim(self) -> int:
        return int(self.head_dim * self.partial_rotary_factor)

    @property
    def gdn_key_dim(self) -> int:
        return self.linear_num_key_heads * self.linear_key_head_dim

    @property
    def gdn_value_dim(self) -> int:
        return self.linear_num_value_heads * self.linear_value_head_dim

    @property
    def gdn_conv_dim(self) -> int:
        return 2 * self.gdn_key_dim + self.gdn_value_dim

    @classmethod
    def from_json(cls, raw: dict) -> TextConfig:
        text = raw.get("text_config", raw)
        if text.get("model_type") not in ("qwen3_5_text", "qwen3_5"):
            raise ValueError(
                f"unsupported model_type {text.get('model_type')!r}; expected qwen3_5_text"
            )
        rope = text["rope_parameters"]
        if rope.get("rope_type", "default") != "default":
            raise ValueError(f"unsupported rope_type {rope['rope_type']!r}")
        if text.get("attn_output_gate") is not True:
            raise ValueError("expected attn_output_gate=true")
        if text.get("mlp_only_layers"):
            raise ValueError("mlp_only_layers is not supported")
        return cls(
            vocab_size=text["vocab_size"],
            hidden_size=text["hidden_size"],
            intermediate_size=text["intermediate_size"],
            num_hidden_layers=text["num_hidden_layers"],
            layer_types=tuple(LayerType(t) for t in text["layer_types"]),
            rms_norm_eps=text["rms_norm_eps"],
            num_attention_heads=text["num_attention_heads"],
            num_key_value_heads=text["num_key_value_heads"],
            head_dim=text["head_dim"],
            rope_theta=float(rope["rope_theta"]),
            partial_rotary_factor=float(rope.get("partial_rotary_factor", 1.0)),
            linear_num_key_heads=text["linear_num_key_heads"],
            linear_num_value_heads=text["linear_num_value_heads"],
            linear_key_head_dim=text["linear_key_head_dim"],
            linear_value_head_dim=text["linear_value_head_dim"],
            linear_conv_kernel_dim=text["linear_conv_kernel_dim"],
            eos_token_id=text["eos_token_id"],
        )


def resolve_model_dir(model: str) -> Path:
    """A local directory, or a HF repo id resolved from the local HF cache (no network)."""
    path = Path(model)
    if path.is_dir():
        return path
    return Path(snapshot_download(model, local_files_only=True))


def load_text_config(model_dir: Path) -> TextConfig:
    return TextConfig.from_json(json.loads((model_dir / "config.json").read_text()))
