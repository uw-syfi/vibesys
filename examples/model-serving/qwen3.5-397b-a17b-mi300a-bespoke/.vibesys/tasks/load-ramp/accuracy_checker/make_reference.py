#!/usr/bin/env python3
"""Generate the numerics gate's reference (``reference/campaign_*.json``) from transformers.

The reference is engine neutral: the transformers ``Qwen3_5MoeForCausalLM`` text model runs
unchanged (eager attention, the torch DeltaNet path, transformers' own ``generate``), on the
served checkpoint ``amd/Qwen3.5-397B-A17B-MXFP4``. Only the routed experts' module is
replaced: transformers cannot load the Quark MXFP4 export, and the experts dequantized to bf16
(773 GB) do not fit in four MI300A (4 x 128 GB). ``PackedExperts`` keeps the packed uint8
weights resident and dequantizes each expert a token routes to when it is used; it computes
exactly what transformers' ``Qwen3_5MoeExperts`` computes, in the same order, on the
dequantized weights. Every other weight is the checkpoint's bf16. Layers are split
contiguously over the given devices.

Run once, on one node with the four APUs and the checkpoint (see ``reference/README.md``):

    python3 accuracy_checker/make_reference.py --model-path <checkpoint dir> \\
        --model-revision <Hugging Face commit of the checkpoint>

For each of the 34 prompts (``reference_prompts``) it renders the chat template with thinking
off, greedy-decodes 32 tokens with every stop token suppressed (``ignore_eos``), then
teacher-forces prompt plus continuation in one forward and records the top-1 token and the
continuation token's logprob at each of the 32 positions. For the first 5 prompts it also
greedy-decodes up to 64 tokens with natural EOS and records the text a server returns for
them (special tokens skipped). Same inputs, same software and same hardware give the same
files; ``reference/provenance.json`` records which of each produced the committed ones.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import random
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import torch
import transformers
from safetensors import safe_open
from torch import nn
from transformers import (
    AutoTokenizer,
    GenerationConfig,
    Qwen3_5MoeForCausalLM,
    Qwen3_5MoeTextConfig,
)
from transformers.activations import ACT2FN
from transformers.models.qwen3_5_moe.modeling_qwen3_5_moe import Qwen3_5MoeTextRotaryEmbedding

sys.path.insert(0, str(Path(__file__).resolve().parent))
# lint-waiver: LW-018709 [E402]; This standalone task script adds its own directory to sys.path before importing its sibling module.
from checker import (  # noqa: E402
    GREEDY_MAX_TOKENS,
    GREEDY_PROMPTS,
    HOLDOUT_WORD_BANK,
    TEACHER_TOP1_TOTAL,
)

TASK_DIR = Path(__file__).resolve().parent.parent
REFERENCE_DIR = TASK_DIR / "reference"
MODEL_ID = "amd/Qwen3.5-397B-A17B-MXFP4"
# The version of the modeling code copied in .vibesys/tasks/default/reference/.
TRANSFORMERS_VERSION = "5.12.1"
# Optional kernel packages transformers prefers over its torch path when they are importable;
# the reference runs the torch path.
ACCELERATED_KERNEL_PACKAGES = ("fla", "causal_conv1d")

CONTINUATION_TOKENS = 32
PROMPTS_SEED = 20260301
N_GENERATED_PROMPTS = 16
LONG_CONTEXT_WORD_COUNTS = (4000, 8000)
CHECKPOINT_PREFIX = "model.language_model."

# The campaign's prompt set (its make_pins.py): 16 static prompts, 16 generated
# "summarize" prompts, and 2 long-context recall prompts.
STATIC_PROMPTS: tuple[list[dict], ...] = (
    [{"role": "user", "content": "Explain in two sentences why the sky is blue."}],
    [
        {
            "role": "user",
            "content": "Write a Python function that returns the n-th Fibonacci number.",
        }
    ],
    [{"role": "user", "content": "List three differences between TCP and UDP."}],
    [{"role": "user", "content": "Translate to French: The meeting has been moved to Thursday."}],
    [{"role": "user", "content": "What is the capital of Australia, and what is it known for?"}],
    [{"role": "user", "content": "Summarize the plot of Romeo and Juliet in one paragraph."}],
    [
        {"role": "user", "content": "My favorite color is green. Please remember it."},
        {"role": "assistant", "content": "Got it, your favorite color is green."},
        {"role": "user", "content": "Suggest a hobby that matches my favorite color."},
    ],
    [
        {"role": "system", "content": "You are a terse assistant."},
        {"role": "user", "content": "Give me a one-line tip for writing clear commit messages."},
    ],
    [
        {
            "role": "user",
            "content": (
                "Write a Rust function `fn is_prime(n: u64) -> bool` using trial division "
                "up to sqrt(n)."
            ),
        }
    ],
    [
        {
            "role": "user",
            "content": (
                "Here is a Python function with a bug:\n\n"
                "def merge_sorted(a, b):\n"
                "    out = []\n"
                "    i = j = 0\n"
                "    while i < len(a) or j < len(b):\n"
                "        if a[i] < b[j]:\n"
                "            out.append(a[i]); i += 1\n"
                "        else:\n"
                "            out.append(b[j]); j += 1\n"
                "    return out\n\n"
                "Find the bug and explain the fix."
            ),
        }
    ],
    [{"role": "user", "content": "Solve for x: 3x^2 - 12x + 9 = 0. Show your work."}],
    [
        {
            "role": "user",
            "content": "A train travels 240 km in 3 hours, then 180 km in 2 hours. "
            "What is its average speed over the whole trip, in km/h?",
        }
    ],
    [{"role": "user", "content": "用中文写一句话描述人工智能的未来。"}],
    [{"role": "user", "content": "Erkläre in einem Satz, was ein neuronales Netz ist."}],
    [{"role": "user", "content": "Escribe un breve poema de dos versos sobre el océano."}],
    [
        {
            "role": "user",
            "content": "日本語で「継続は力なり」ということわざの意味を説明してください。",
        }
    ],
)


def reference_prompts() -> list[list[dict]]:
    """The 34 scored prompts, in order; the first ``GREEDY_PROMPTS`` are also the greedy ones."""
    prompts = [list(p) for p in STATIC_PROMPTS]
    for index in range(N_GENERATED_PROMPTS):
        rng = random.Random(f"{PROMPTS_SEED}-{index}")
        words = " ".join(rng.choice(HOLDOUT_WORD_BANK) for _ in range(rng.randint(60, 240)))
        prompts.append(
            [{"role": "user", "content": f"Summarize this text in one sentence: {words}."}]
        )
    for index, n_words in enumerate(LONG_CONTEXT_WORD_COUNTS):
        rng = random.Random(f"{PROMPTS_SEED}-long-{index}")
        fact = f"The archive reference code for this document is ZL-{9000 + index}."
        filler = " ".join(rng.choice(HOLDOUT_WORD_BANK) for _ in range(n_words))
        content = (
            f"{fact} {filler} What was the archive reference code mentioned at the "
            "start of this document? Answer with just the code."
        )
        prompts.append([{"role": "user", "content": content}])
    return prompts


# --- MXFP4 ----------------------------------------------------------------------------------

MXFP4_BLOCK = 32
# OCP MX fp4 e2m1: bit 3 is the sign, bits 0-2 index the magnitudes.
_FP4_VALUES = (0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0)
_FP4_TABLE = (*_FP4_VALUES, *(-v for v in _FP4_VALUES))
E8M0_NAN = 255


def dequant_mxfp4(packed: torch.Tensor, scale: torch.Tensor, dtype: torch.dtype) -> torch.Tensor:
    """The dense [..., K] weight of packed uint8 [..., K/2] and e8m0 scales [..., K/32].

    The low nibble is the even element (checked against the FP8 source checkpoint, see
    .vibesys/tasks/default/reference/README.md). fp4 values times a power of two are exact in
    bf16, so the result is the stored weight exactly.
    """
    lut = torch.tensor(_FP4_TABLE, dtype=torch.float32, device=packed.device)
    values = torch.stack([lut[(packed & 0xF).long()], lut[(packed >> 4).long()]], dim=-1)
    scales = torch.exp2(scale.float() - 127.0).repeat_interleave(MXFP4_BLOCK, dim=-1)
    return (values.flatten(-2) * scales).to(dtype)


class ShardReader:
    """Tensor name -> shard, from the shard headers of a directory of safetensors files."""

    def __init__(self, directory: Path) -> None:
        files = sorted(directory.glob("*.safetensors"))
        if not files:
            raise FileNotFoundError(f"{directory}: no *.safetensors shards")
        self._handles = {f: safe_open(str(f), framework="pt", device="cpu") for f in files}
        # lint-waiver: LW-018710 [SIM118]; a safetensors handle lists its tensors only through keys().
        # > It supports neither iteration nor `in`, so the form SIM118 asks for raises TypeError.
        self._owner = {name: f for f, h in self._handles.items() for name in h.keys()}  # noqa: SIM118

    def load(self, name: str, device: torch.device, dtype: torch.dtype | None) -> torch.Tensor:
        if name not in self._owner:
            raise KeyError(f"the checkpoint has no tensor {name}")
        tensor = self._handles[self._owner[name]].get_tensor(name)
        return tensor.to(device=device, dtype=dtype or tensor.dtype)


class PackedExperts(nn.Module):
    """transformers' ``Qwen3_5MoeExperts`` over packed MXFP4 weights, dequantized on use."""

    def __init__(
        self, reader: ShardReader, prefix: str, config, device: torch.device, dtype: torch.dtype
    ) -> None:
        super().__init__()
        self.num_experts, self.dtype = config.num_experts, dtype
        self.act_fn = ACT2FN[config.hidden_act]
        self.gate_up = self._stack(reader, prefix, ("gate_proj", "up_proj"), "weight", device)
        self.gate_up_scale = self._stack(
            reader, prefix, ("gate_proj", "up_proj"), "weight_scale", device
        )
        self.down = self._stack(reader, prefix, ("down_proj",), "weight", device)
        self.down_scale = self._stack(reader, prefix, ("down_proj",), "weight_scale", device)
        if bool((self.gate_up_scale == E8M0_NAN).any() or (self.down_scale == E8M0_NAN).any()):
            raise ValueError(f"{prefix}: an e8m0 scale is NaN (255)")

    def _stack(
        self,
        reader: ShardReader,
        prefix: str,
        projections: tuple[str, ...],
        suffix: str,
        device: torch.device,
    ) -> torch.Tensor:
        def read(expert: int) -> torch.Tensor:
            parts = [
                reader.load(f"{prefix}.{expert}.{p}.{suffix}", device, None) for p in projections
            ]
            return torch.cat(parts, 0)

        first = read(0)
        out = torch.empty((self.num_experts, *first.shape), dtype=first.dtype, device=device)
        out[0] = first
        for expert in range(1, self.num_experts):
            out[expert] = read(expert)
        return out

    def forward(
        self, hidden_states: torch.Tensor, top_k_index: torch.Tensor, top_k_weights: torch.Tensor
    ) -> torch.Tensor:
        final = torch.zeros_like(hidden_states)
        with torch.no_grad():
            mask = nn.functional.one_hot(top_k_index, num_classes=self.num_experts).permute(2, 1, 0)
            hit = torch.greater(mask.sum(dim=(-1, -2)), 0).nonzero()
        for expert in hit[:, 0].tolist():
            top_k_pos, token_idx = torch.where(mask[expert])
            gate_up = dequant_mxfp4(self.gate_up[expert], self.gate_up_scale[expert], self.dtype)
            down = dequant_mxfp4(self.down[expert], self.down_scale[expert], self.dtype)
            gate, up = nn.functional.linear(hidden_states[token_idx], gate_up).chunk(2, dim=-1)
            current = nn.functional.linear(self.act_fn(gate) * up, down)
            current = current * top_k_weights[token_idx, top_k_pos, None]
            final.index_add_(0, token_idx, current.to(final.dtype))
        return final


def _to_device(value: object, device: torch.device) -> object:
    if isinstance(value, torch.Tensor):
        return value.to(device)
    if isinstance(value, tuple | list):
        return type(value)(_to_device(v, device) for v in value)
    return value


def _move_inputs_to(device: torch.device) -> Callable:
    def hook(_module: nn.Module, args: tuple, kwargs: dict) -> tuple[tuple, dict]:
        return _to_device(args, device), {k: _to_device(v, device) for k, v in kwargs.items()}

    return hook


def load_reference_model(
    model_dir: Path, devices: Sequence[torch.device], dtype: torch.dtype
) -> nn.Module:
    """transformers' text model on the checkpoint, experts packed, layers split over devices."""

    raw = json.loads((model_dir / "config.json").read_text())
    text = raw.get("text_config", raw)
    kwargs = {k: v for k, v in text.items() if k not in ("model_type", "attn_implementation")}
    config = Qwen3_5MoeTextConfig(**kwargs, attn_implementation="eager")
    if config.tie_word_embeddings:
        raise ValueError(f"{model_dir}: expected an untied lm_head")
    reader = ShardReader(model_dir)
    with torch.device("meta"):
        model = Qwen3_5MoeForCausalLM(config)
    n_layers = config.num_hidden_layers
    layer_device = [devices[i * len(devices) // n_layers] for i in range(n_layers)]

    def device_of(name: str) -> torch.device:
        if name.startswith("model.layers."):
            return layer_device[int(name.split(".")[2])]
        return devices[0] if name.startswith("model.embed_tokens.") else devices[-1]

    for index, layer in enumerate(model.model.layers):
        prefix = f"{CHECKPOINT_PREFIX}layers.{index}.mlp.experts"
        layer.mlp.experts = PackedExperts(reader, prefix, config, layer_device[index], dtype)
        layer.register_forward_pre_hook(_move_inputs_to(layer_device[index]), with_kwargs=True)
    for name, param in list(model.named_parameters()):
        source = name if name == "lm_head.weight" else name.replace("model.", CHECKPOINT_PREFIX, 1)
        tensor = reader.load(source, device_of(name), dtype)
        if tensor.shape != param.shape:
            raise ValueError(
                f"{source}: shape {tuple(tensor.shape)}, expected {tuple(param.shape)}"
            )
        owner, _, leaf = name.rpartition(".")
        setattr(model.get_submodule(owner), leaf, nn.Parameter(tensor, requires_grad=False))
    model.model.rotary_emb = Qwen3_5MoeTextRotaryEmbedding(config, device=devices[0])
    model.model.norm.register_forward_pre_hook(_move_inputs_to(devices[-1]), with_kwargs=True)
    model.lm_head.register_forward_hook(lambda _m, _a, out: out.to(devices[0]))
    leftover = [n for n, t in (*model.named_parameters(), *model.named_buffers()) if t.is_meta]
    if leftover:
        raise ValueError(f"not loaded from the checkpoint: {leftover[:5]}")
    return model.eval()


# --- The reference ---------------------------------------------------------------------------


def stop_token_ids(model_dir: Path, tokenizer) -> list[int]:
    """Every id a server stops on: config, generation config and tokenizer EOS."""
    ids: set[int] = set()
    for name in ("config.json", "generation_config.json"):
        path = model_dir / name
        if path.exists():
            data = json.loads(path.read_text())
            for section in (data, data.get("text_config") or {}):
                eos = section.get("eos_token_id")
                ids |= {eos} if isinstance(eos, int) else set(eos or ())
    if tokenizer.eos_token_id is not None:
        ids.add(tokenizer.eos_token_id)
    return sorted(ids)


def generate(
    model: nn.Module, prompt_ids: list[int], max_new: int, *, min_new: int, stop: Sequence[int]
) -> list[int]:

    # A fresh config, so no sampling default of the checkpoint's generation_config applies.
    config = GenerationConfig(
        do_sample=False,
        max_new_tokens=max_new,
        min_new_tokens=min_new,
        eos_token_id=list(stop),
        pad_token_id=stop[0],
    )
    device = next(model.parameters()).device
    ids = torch.tensor([prompt_ids], device=device)
    with torch.no_grad():
        out = model.generate(
            input_ids=ids, attention_mask=torch.ones_like(ids), generation_config=config
        )
    return out[0, len(prompt_ids) :].tolist()


def teacher_forced(
    model: nn.Module, prompt_ids: list[int], continuation: list[int]
) -> tuple[list[int], list[float]]:
    """(top-1 token, logprob of the continuation token) at each continuation position."""
    device = next(model.parameters()).device
    ids = torch.tensor([[*prompt_ids, *continuation]], device=device)
    with torch.no_grad():
        logits = model(input_ids=ids, logits_to_keep=len(continuation) + 1).logits[0, :-1]
    logprobs = torch.log_softmax(logits.float(), dim=-1)
    top1 = logprobs.argmax(dim=-1).tolist()
    chosen = logprobs.gather(-1, torch.tensor(continuation, device=logprobs.device)[:, None])
    return top1, chosen[:, 0].tolist()


def prompt_token_ids(tokenizer, messages: list[dict]) -> list[int]:
    text = tokenizer.apply_chat_template(
        messages, add_generation_prompt=True, enable_thinking=False, tokenize=False
    )
    return tokenizer(text, add_special_tokens=False)["input_ids"]


def build_reference(
    model: nn.Module, tokenizer, stop: Sequence[int], prompts: Sequence[list[dict]]
) -> dict[str, dict]:
    """The three reference files' contents, keyed by file name, in the campaign's format."""
    continuations, scores, greedy = [], [], []
    for index, messages in enumerate(prompts):
        pin_id = f"pin{index:02d}"
        prompt_ids = prompt_token_ids(tokenizer, messages)
        continuation = generate(
            model, prompt_ids, CONTINUATION_TOKENS, min_new=CONTINUATION_TOKENS, stop=stop
        )
        top1, logprobs = teacher_forced(model, prompt_ids, continuation)
        continuations.append(
            {
                "id": pin_id,
                "messages": messages,
                "prompt_ids": prompt_ids,
                "continuation_ids": continuation,
            }
        )
        scores.append({"id": pin_id, "ok": True, "logprobs": logprobs, "top1": top1})
        print(f"{pin_id}: {tokenizer.decode(continuation)[:70]!r}", file=sys.stderr, flush=True)
    for index, messages in enumerate(prompts[:GREEDY_PROMPTS]):
        ids = generate(
            model, prompt_token_ids(tokenizer, messages), GREEDY_MAX_TOKENS, min_new=0, stop=stop
        )
        ids = ids[: next((i for i, t in enumerate(ids) if t in stop), len(ids))]
        text = tokenizer.decode(ids, skip_special_tokens=True)
        greedy.append({"id": f"greedy{index:02d}", "text": text, "token_ids": ids})
    return {
        "campaign_continuations.json": {"pins": continuations},
        "campaign_scores.json": {"pins": scores},
        "campaign_greedy.json": {"prompts": greedy},
    }


def write_reference(files: dict[str, dict], directory: Path) -> dict[str, str]:
    """Write each file as compact JSON; return each one's SHA-256."""
    directory.mkdir(parents=True, exist_ok=True)
    digests = {}
    for name, payload in files.items():
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
        (directory / name).write_bytes(data)
        digests[name] = hashlib.sha256(data).hexdigest()
    return digests


def _checkpoint_fingerprint(model_dir: Path) -> dict:
    """The checkpoint's config digest and shard names and sizes; no local path."""
    return {
        "config_sha256": hashlib.sha256((model_dir / "config.json").read_bytes()).hexdigest(),
        "shards": {f.name: f.stat().st_size for f in sorted(model_dir.glob("*.safetensors"))},
    }


def _check_environment(*, allow_other_transformers: bool) -> None:
    if transformers.__version__ != TRANSFORMERS_VERSION and not allow_other_transformers:
        raise SystemExit(
            f"transformers {transformers.__version__} is installed; the reference is made with "
            f"{TRANSFORMERS_VERSION} (pass --allow-other-transformers to override)"
        )
    present = [p for p in ACCELERATED_KERNEL_PACKAGES if importlib.util.find_spec(p)]
    if present:
        raise SystemExit(f"uninstall {present}: the reference runs transformers' torch path")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model-path", required=True, type=Path, help="The checkpoint directory.")
    parser.add_argument(
        "--model-revision", required=True, help=f"The Hugging Face commit of {MODEL_ID}."
    )
    parser.add_argument("--out-dir", type=Path, default=REFERENCE_DIR)
    parser.add_argument("--dtype", choices=["bfloat16"], default="bfloat16")
    parser.add_argument("--allow-other-transformers", action="store_true")
    args = parser.parse_args(argv)
    _check_environment(allow_other_transformers=args.allow_other_transformers)
    # Deterministic BLAS workspaces, set before the first GPU call.
    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    torch.use_deterministic_algorithms(True, warn_only=True)
    count = torch.cuda.device_count()
    devices = [torch.device(f"cuda:{i}") for i in range(count)] or [torch.device("cpu")]
    tokenizer = AutoTokenizer.from_pretrained(args.model_path)
    model = load_reference_model(args.model_path, devices, getattr(torch, args.dtype))
    prompts = reference_prompts()
    files = build_reference(model, tokenizer, stop_token_ids(args.model_path, tokenizer), prompts)
    positions = sum(len(p["top1"]) for p in files["campaign_scores.json"]["pins"])
    if positions != TEACHER_TOP1_TOTAL:
        raise SystemExit(f"scored {positions} positions, the gate needs {TEACHER_TOP1_TOTAL}")
    digests = write_reference(files, args.out_dir)
    provenance = {
        "model_id": MODEL_ID,
        "model_revision": args.model_revision,
        "checkpoint": _checkpoint_fingerprint(args.model_path),
        "dtype": args.dtype,
        "experts": "MXFP4, dequantized exactly to bf16 on use (PackedExperts)",
        "attention": "eager",
        "versions": {
            "python": platform.python_version(),
            "torch": torch.__version__,
            "hip": torch.version.hip,
            "transformers": transformers.__version__,
        },
        "devices": [torch.cuda.get_device_name(i) for i in range(count)] or ["cpu"],
        "command": "python3 accuracy_checker/make_reference.py --model-path <checkpoint> "
        f"--model-revision {args.model_revision}",
        "sha256": digests,
    }
    (args.out_dir / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(json.dumps(provenance, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
