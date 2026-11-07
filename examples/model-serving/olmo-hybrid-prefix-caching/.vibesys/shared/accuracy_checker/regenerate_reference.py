# /// script
# requires-python = ">=3.12"
# dependencies = ["torch==2.15.0", "transformers==5.22.2", "accelerate", "huggingface_hub"]
# ///
"""Regenerate ``reference_outputs.json``, the outputs the accuracy checker compares against.

The reference is Hugging Face ``transformers`` greedy generation: bfloat16, eager
attention, ``generate(max_new_tokens=N, do_sample=False)``, decoded with
``skip_special_tokens=True``. Olmo-Hybrid's linear-attention layers run either
plain-torch kernels or flash-linear-attention kernels, chosen by whether the
``flash-linear-attention`` package is installed, and the two can produce different
bfloat16 greedy outputs. So the reference has one variant per kernel choice, and the
checker accepts either text for a prompt.

Run ``generate`` once per variant on the GPU the task targets, then ``merge``::

    uv run --script regenerate_reference.py generate \\
        --model allenai/Olmo-Hybrid-7B --revision <sha> --output torch.json
    uv run --with flash-linear-attention==0.6.0 --script regenerate_reference.py generate \\
        --model allenai/Olmo-Hybrid-7B --revision <sha> --output fla.json
    python regenerate_reference.py merge torch.json fla.json --output reference_outputs.json

``generate`` downloads the pinned model revision and needs one CUDA GPU with room
for the bfloat16 model (about 15 GB). ``merge`` needs only the standard library.

Public API: ``PROMPTS``, ``VARIANTS``, ``generate`` and ``merge``.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

#: (prompt, max_new_tokens, description), in checker order. Olmo-Hybrid-7B is a base
#: model, so the prompts are plain completions with no chat template.
PROMPTS: tuple[tuple[str, int, str], ...] = (
    ("The capital of France is", 15, "short factual completion"),
    ("Once upon a time, in a land far away,", 50, "story continuation"),
    ('def fibonacci(n):\n    """Return the n-th Fibonacci number."""\n', 40, "code completion"),
    ("1 + 1 =", 5, "arithmetic"),
    ("A B C D E F G H I J K L M N O P Q R S T U V W X Y Z A B C D E F G", 20, "alphabet pattern"),
    (
        "The following is a detailed explanation of how neural networks work. "
        "Neural networks are computing systems inspired by biological neural networks. "
        "They consist of layers of interconnected nodes or neurons. "
        "Each connection has a weight that adjusts as learning proceeds. "
        "The network processes information using a connectionist approach. "
        "In summary, the key takeaway is that",
        30,
        "long prompt completion",
    ),
    ("Question: What is the speed of light?\nAnswer:", 20, "Q&A format"),
    ("The year 2024 was followed by the year", 10, "number continuation"),
    ("Hello", 15, "single word prompt"),
    ('{"name": "Alice", "age":', 10, "JSON completion"),
)

#: The kernel choices a reference variant can come from, in the order the merged file
#: lists them.
VARIANTS = ("torch", "flash_linear_attention")

#: How every variant generates, recorded in the merged file.
GENERATION = {
    "dtype": "bfloat16",
    "attn_implementation": "eager",
    "decoding": "greedy: generate(max_new_tokens=max_tokens, do_sample=False)",
    "text": "tokenizer.decode(output_token_ids, skip_special_tokens=True)",
}


def generate(model_id: str, revision: str, cache_dir: str | None = None) -> dict[str, object]:
    """Generate one variant's reference on ``cuda:0``; the variant is the installed kernel."""
    import torch
    import transformers
    from huggingface_hub import snapshot_download
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from transformers.utils.import_utils import is_flash_linear_attention_available

    device = "cuda:0"
    model_dir = snapshot_download(model_id, revision=revision, cache_dir=cache_dir)
    tokenizer = AutoTokenizer.from_pretrained(model_dir)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        model_dir,
        torch_dtype=torch.bfloat16,
        device_map=device,
        attn_implementation=GENERATION["attn_implementation"],
    )
    model.eval()
    fla = bool(is_flash_linear_attention_available())
    fla_version = importlib.metadata.version("flash-linear-attention") if fla else None
    cases = []
    with torch.inference_mode():
        for prompt, max_tokens, description in PROMPTS:
            inputs = tokenizer(prompt, return_tensors="pt").to(device)
            prompt_ids = inputs.input_ids[0].tolist()
            output = model.generate(**inputs, max_new_tokens=max_tokens, do_sample=False)
            ids = output[0, len(prompt_ids) :].tolist()
            cases.append(
                {
                    "description": description,
                    "prompt": prompt,
                    "max_tokens": max_tokens,
                    "prompt_token_ids": prompt_ids,
                    "output_token_ids": ids,
                    "output_text": tokenizer.decode(ids, skip_special_tokens=True),
                }
            )
    return {
        "variant": "flash_linear_attention" if fla else "torch",
        "model": {"id": model_id, "revision": revision},
        "environment": {
            "gpu": torch.cuda.get_device_name(0),
            "torch": str(torch.__version__),
            "transformers": str(transformers.__version__),
            "flash_linear_attention": fla_version,
        },
        "cases": cases,
    }


def merge(raws: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Combine ``generate`` outputs, one per variant, into the checker's reference file.

    Every input must come from the same model revision and hold the ``PROMPTS`` with
    identical prompt token IDs; a mismatch raises ``ValueError`` naming it.
    """
    by_variant = {}
    for raw in raws:
        variant = raw["variant"]
        if variant not in VARIANTS:
            raise ValueError(f"unknown variant {variant!r}; expected one of {VARIANTS}")
        if variant in by_variant:
            raise ValueError(f"two inputs are the {variant!r} variant")
        by_variant[variant] = raw
    if not by_variant:
        raise ValueError("merge needs at least one generate output")
    variants = [name for name in VARIANTS if name in by_variant]
    first = by_variant[variants[0]]
    for name in variants:
        raw = by_variant[name]
        if raw["model"] != first["model"]:
            raise ValueError(f"variant {name!r} used model {raw['model']}, not {first['model']}")
        shapes = [(c["prompt"], c["max_tokens"], c["description"]) for c in raw["cases"]]
        if shapes != list(PROMPTS):
            raise ValueError(f"variant {name!r} does not hold PROMPTS in order")
    merged_cases = []
    for index, (prompt, max_tokens, description) in enumerate(PROMPTS):
        rows = {name: by_variant[name]["cases"][index] for name in variants}
        prompt_ids = rows[variants[0]]["prompt_token_ids"]
        for name, row in rows.items():
            if row["prompt_token_ids"] != prompt_ids:
                raise ValueError(f"variant {name!r} tokenized {description!r} differently")
        merged_cases.append(
            {
                "description": description,
                "prompt": prompt,
                "max_tokens": max_tokens,
                "prompt_token_ids": prompt_ids,
                "outputs": {
                    name: {"token_ids": row["output_token_ids"], "text": row["output_text"]}
                    for name, row in rows.items()
                },
            }
        )
    return {
        "model": first["model"],
        "generation": GENERATION,
        "variants": {name: by_variant[name]["environment"] for name in variants},
        "cases": merged_cases,
    }


def _write(path: Path, document: Mapping[str, object]) -> None:
    path.write_text(json.dumps(document, indent=1, ensure_ascii=False) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    gen = commands.add_parser("generate", help="generate one variant on the local GPU")
    gen.add_argument("--model", required=True, help="Hugging Face model id")
    gen.add_argument("--revision", required=True, help="full commit SHA of the model")
    gen.add_argument("--cache-dir", default=None, help="Hugging Face cache directory")
    gen.add_argument("--output", type=Path, required=True)
    mrg = commands.add_parser("merge", help="merge generate outputs into the reference file")
    mrg.add_argument("inputs", type=Path, nargs="+")
    mrg.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "generate":
        if len(args.revision) != 40 or any(c not in "0123456789abcdef" for c in args.revision):
            parser.error("--revision must be a full lowercase commit SHA")
        raw = generate(args.model, args.revision, args.cache_dir)
        _write(args.output, raw)
        print(f"{raw['variant']} {json.dumps(raw['environment'])} -> {args.output}")
        return 0
    try:
        document = merge([json.loads(path.read_text()) for path in args.inputs])
    except (KeyError, ValueError) as exc:
        print(f"merge failed: {exc}", file=sys.stderr)
        return 1
    _write(args.output, document)
    print(f"{', '.join(document['variants'])} -> {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
