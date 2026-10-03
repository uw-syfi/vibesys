"""A randomly initialized, tiny Qwen3.5 checkpoint with the 9B model's architecture.

Same layer pattern (Gated DeltaNet layers with a full-attention layer every
`full_attention_interval`), the same config keys, tensor names, and file
layout as the HF checkpoint, so a candidate's `--model <dir>` path loads it
unchanged. Only the sizes differ; nothing here is a trained weight.

The LM head is scaled up so greedy decoding has large top-1 margins: bf16
rounding differences between two correct implementations then rarely flip a
token, and a flip at a near-tie is still told apart by the reference margin.
"""

from __future__ import annotations

import json
from pathlib import Path

import torch
from reference.config import TextConfig
from reference.model import Qwen35ForCausalLM
from safetensors.torch import save_file
from tokenizers import Tokenizer, decoders, models, pre_tokenizers, trainers

VOCAB_SIZE = 512
FULL_ATTENTION_INTERVAL = 4
NUM_LAYERS = 8  # two full-attention layers (3, 7), six GDN layers
LM_HEAD_STD = 2.0  # logit std ~ 2 * sqrt(hidden) = 16 nats
_WEIGHTS_FILE = "model.safetensors"
_TEXT_PREFIX = "model.language_model."


def text_config_json() -> dict:
    """`text_config` of the tiny checkpoint, keyed like the 9B `config.json`."""
    return {
        "model_type": "qwen3_5_text",
        "vocab_size": VOCAB_SIZE,
        "hidden_size": 64,
        "intermediate_size": 128,
        "num_hidden_layers": NUM_LAYERS,
        "full_attention_interval": FULL_ATTENTION_INTERVAL,
        "layer_types": [
            "full_attention" if (i + 1) % FULL_ATTENTION_INTERVAL == 0 else "linear_attention"
            for i in range(NUM_LAYERS)
        ],
        "rms_norm_eps": 1e-6,
        "num_attention_heads": 4,
        "num_key_value_heads": 2,
        "head_dim": 32,
        "attn_output_gate": True,
        "rope_parameters": {
            "rope_type": "default",
            "rope_theta": 10000000.0,
            "partial_rotary_factor": 0.25,
            "mrope_section": [2, 1, 1],
            "mrope_interleaved": True,
        },
        "linear_num_key_heads": 2,
        "linear_num_value_heads": 4,
        "linear_key_head_dim": 16,
        "linear_value_head_dim": 16,
        "linear_conv_kernel_dim": 4,
        "hidden_act": "silu",
        "max_position_embeddings": 4096,
        "tie_word_embeddings": False,
        "dtype": "bfloat16",
        "eos_token_id": VOCAB_SIZE - 1,
        "bos_token_id": None,
        "pad_token_id": None,
    }


def _tokenizer() -> Tokenizer:
    """Byte-level BPE, the 9B tokenizer's family, so `AutoTokenizer` loads it as Qwen's class.

    Its vocabulary is smaller than the model's; decoding skips ids it lacks, as
    it does for the 9B checkpoint's padded vocabulary rows.
    """
    tok = Tokenizer(models.BPE())
    tok.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    tok.decoder = decoders.ByteLevel()
    trainer = trainers.BpeTrainer(
        vocab_size=VOCAB_SIZE,
        initial_alphabet=pre_tokenizers.ByteLevel.alphabet(),
        show_progress=False,
    )
    tok.train_from_iterator(["def main():\n    return json.dumps({'x': 1})\n"] * 4, trainer)
    return tok


def write_checkpoint(model_dir: Path, seed: int = 0) -> TextConfig:
    """Write config, tokenizer, and random bf16 weights to `model_dir`; return its config."""
    model_dir.mkdir(parents=True, exist_ok=True)
    text = text_config_json()
    config = {
        "model_type": "qwen3_5",
        "architectures": ["Qwen3_5ForConditionalGeneration"],
        "text_config": text,
        "tie_word_embeddings": False,
    }
    (model_dir / "config.json").write_text(json.dumps(config, indent=2))
    cfg = TextConfig.from_json(config)

    torch.manual_seed(seed)
    model = Qwen35ForCausalLM(cfg)
    with torch.no_grad():
        model.lm_head.weight.normal_(0.0, LM_HEAD_STD)
        for name, param in model.named_parameters():
            if name.endswith("A_log"):  # decay rates spread over (0.5, 4] like trained gates
                param.uniform_(-0.7, 1.4)
    tensors = {
        (name if name == "lm_head.weight" else _TEXT_PREFIX + name): t.detach()
        .to(torch.bfloat16)
        .contiguous()
        for name, t in model.state_dict().items()
    }
    save_file(tensors, str(model_dir / _WEIGHTS_FILE), metadata={"format": "pt"})
    index = {"metadata": {}, "weight_map": dict.fromkeys(sorted(tensors), _WEIGHTS_FILE)}
    (model_dir / "model.safetensors.index.json").write_text(json.dumps(index, indent=2))

    _tokenizer().save(str(model_dir / "tokenizer.json"))
    tokenizer_config = {
        "eos_token": "<|im_end|>",
        "model_max_length": text["max_position_embeddings"],
    }
    (model_dir / "tokenizer_config.json").write_text(json.dumps(tokenizer_config, indent=2))
    return cfg
