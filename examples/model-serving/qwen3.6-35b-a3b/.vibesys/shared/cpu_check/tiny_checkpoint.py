"""A randomly initialized, tiny checkpoint with Qwen3.6-35B-A3B's architecture.

Same model type (``qwen3_5_moe``, the vision-language wrapper the real
checkpoint uses), the same hybrid Gated DeltaNet / gated-attention layer
pattern and the same routed-plus-shared expert MoE, so the engine loads it
through the same Transformers path. Only the sizes differ; nothing is trained.
The tokenizer is a small byte-level BPE with the chat special tokens and a chat
template that keeps the real template's switches (``enable_thinking``,
tools, tool-call arguments as a mapping, reasoning history).
"""

from __future__ import annotations

from pathlib import Path

import torch
from tokenizers import Tokenizer, decoders, models, pre_tokenizers, trainers
from transformers import (
    GenerationConfig,
    PreTrainedTokenizerFast,
    Qwen3_5MoeConfig,
    Qwen3_5MoeForConditionalGeneration,
)

SPECIAL = ["<|endoftext|>", "<|im_start|>", "<|im_end|>", "<think>", "</think>"]
VOCAB_SIZE = 320
CHAT_TEMPLATE = """
{%- if tools %}<|im_start|>system
# Tools
{% for tool in tools %}{{ tool | tojson }}
{% endfor %}<|im_end|>
{% endif %}
{%- for message in messages %}
{%- if message.role == 'assistant' %}<|im_start|>assistant
{%- if message.reasoning_content is string %}
<think>
{{ message.reasoning_content }}
</think>
{% endif %}
{{ message.content or '' }}
{%- for call in message.tool_calls or [] %}
{%- set call = call.function if call.function is defined else call %}
<tool_call>
<function={{ call.name }}>
{%- for name, value in call.arguments | items %}
<parameter={{ name }}>
{{ value if value is string else value | tojson }}
</parameter>
{%- endfor %}
</function>
</tool_call>
{%- endfor %}<|im_end|>
{% elif message.content is string %}<|im_start|>{{ message.role }}
{{ message.content }}<|im_end|>
{% else %}<|im_start|>{{ message.role }}
{% for part in message.content %}{{ part.text }}{% endfor %}<|im_end|>
{% endif %}
{%- endfor %}
{%- if add_generation_prompt %}<|im_start|>assistant
{% if enable_thinking is defined and enable_thinking is false %}<think>

</think>

{% else %}<think>
{% endif %}
{%- endif %}
"""


def _tokenizer() -> PreTrainedTokenizerFast:
    tok = Tokenizer(models.BPE())
    tok.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    tok.decoder = decoders.ByteLevel()
    trainer = trainers.BpeTrainer(
        vocab_size=VOCAB_SIZE - len(SPECIAL),
        initial_alphabet=pre_tokenizers.ByteLevel.alphabet(),
        special_tokens=SPECIAL,
        show_progress=False,
    )
    corpus = ["def main():\n    return sorted(set(items))  # the capital of France is Paris\n"] * 8
    tok.train_from_iterator(corpus, trainer)
    return PreTrainedTokenizerFast(
        tokenizer_object=tok,
        eos_token="<|im_end|>",
        pad_token="<|endoftext|>",
        additional_special_tokens=["<|im_start|>"],
        chat_template=CHAT_TEMPLATE,
    )


def write_checkpoint(model_dir: Path, *, seed: int = 0) -> None:
    """Write config, tokenizer and random BF16 weights to ``model_dir``."""
    tokenizer = _tokenizer()
    eos_ids = [
        tokenizer.convert_tokens_to_ids("<|im_end|>"),
        tokenizer.convert_tokens_to_ids("<|endoftext|>"),
    ]
    text = {
        "vocab_size": VOCAB_SIZE,
        "hidden_size": 64,
        "num_hidden_layers": 4,
        "layer_types": ["linear_attention", "full_attention"] * 2,
        "full_attention_interval": 2,
        "num_attention_heads": 4,
        "num_key_value_heads": 2,
        "head_dim": 32,
        "linear_num_key_heads": 2,
        "linear_num_value_heads": 4,
        "linear_key_head_dim": 16,
        "linear_value_head_dim": 16,
        "linear_conv_kernel_dim": 4,
        "num_experts": 8,
        "num_experts_per_tok": 2,
        "moe_intermediate_size": 32,
        "shared_expert_intermediate_size": 32,
        "max_position_embeddings": 4096,
        "eos_token_id": eos_ids[0],
        "rope_parameters": {
            "rope_type": "default",
            "rope_theta": 10000000,
            "partial_rotary_factor": 0.25,
            "mrope_section": [2, 1, 1],
            "mrope_interleaved": True,
        },
    }
    vision = {
        "depth": 1,
        "hidden_size": 32,
        "intermediate_size": 32,
        "num_heads": 2,
        "out_hidden_size": 64,
        "patch_size": 16,
        "spatial_merge_size": 2,
        "temporal_patch_size": 2,
    }
    torch.manual_seed(seed)
    model = Qwen3_5MoeForConditionalGeneration(
        Qwen3_5MoeConfig(text_config=text, vision_config=vision)
    )
    with torch.no_grad():
        # Large logit margins keep greedy choices far from ties.
        model.lm_head.weight.normal_(0.0, 2.0)
    model.to(torch.bfloat16).save_pretrained(model_dir)
    tokenizer.save_pretrained(model_dir)
    set_eos(model_dir, eos_ids)


def set_eos(model_dir: Path, eos_ids: list[int]) -> None:
    """Declare which ids end a sequence, as the real checkpoint's generation config does."""
    GenerationConfig(eos_token_id=eos_ids, bos_token_id=None).save_pretrained(model_dir)
