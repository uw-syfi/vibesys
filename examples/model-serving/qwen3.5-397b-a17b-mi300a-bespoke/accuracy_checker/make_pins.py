#!/usr/bin/env python3
"""Generate ``reference/pins.json`` from a Hugging Face transformers reference forward.

Run once on the cluster (needs the weights and GPUs or enough host memory):

    python3 accuracy_checker/make_pins.py --model-path "$MODEL_PATH"

For each fixed prompt it applies the chat template with thinking disabled,
greedy-decodes exactly ``--n-tokens`` tokens (EOS suppressed, matching the
benchmark's ``ignore_eos``), one prompt at a time (no padding effects), and
records the token ids. The output schema is documented in
``accuracy_checker/README.md``.

The pinned checkpoint is normally the same one candidates serve. If
transformers cannot load it (for example a quantized MXFP4 export), pass
``--model-path`` pointing at a loadable copy of the same model (bf16 or FP8)
and ``--tokenizer-path`` pointing at the served checkpoint; the checker's
budget tolerates the resulting numeric noise but not a different model.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

BUNDLE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
# lint-waiver: LW-008011 [E402]; This standalone bundle adds a sibling module directory to sys.path before importing its modules.
from checker import EXACT_PREFIX_TOKENS, HOLDOUT_WORD_BANK, PINS_SCHEMA_VERSION  # noqa: E402

PINS_SEED = 20260301  # distinct from the benchmark and holdout seeds.

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
)
N_GENERATED_PROMPTS = 8


def generated_prompts() -> list[list[dict]]:
    """Longer word-bank prompts, deterministic, so pins also cover multi-hundred-token prefills."""
    prompts = []
    for index in range(N_GENERATED_PROMPTS):
        rng = random.Random(f"{PINS_SEED}-{index}")
        words = " ".join(rng.choice(HOLDOUT_WORD_BANK) for _ in range(rng.randint(60, 240)))
        prompts.append(
            [{"role": "user", "content": f"Summarize this text in one sentence: {words}."}]
        )
    return prompts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", required=True, help="Checkpoint transformers loads.")
    parser.add_argument("--tokenizer-path", default=None, help="Defaults to --model-path.")
    parser.add_argument("--n-tokens", type=int, default=EXACT_PREFIX_TOKENS)
    parser.add_argument("--dtype", default="bfloat16")
    parser.add_argument("--output", default=str(BUNDLE_DIR / "reference" / "pins.json"))
    args = parser.parse_args()
    if args.n_tokens < EXACT_PREFIX_TOKENS:
        parser.error(f"--n-tokens must be >= {EXACT_PREFIX_TOKENS} (the checker's prefix length)")

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        args.tokenizer_path or args.model_path, trust_remote_code=True
    )
    model = AutoModelForCausalLM.from_pretrained(
        args.model_path,
        torch_dtype=getattr(torch, args.dtype),
        device_map="auto",
        trust_remote_code=True,
    )
    model.eval()

    pins = []
    for index, messages in enumerate((*STATIC_PROMPTS, *generated_prompts())):
        prompt = tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, enable_thinking=False, tokenize=False
        )
        inputs = tokenizer(prompt, return_tensors="pt", add_special_tokens=False).to(model.device)
        with torch.no_grad():
            out = model.generate(
                **inputs,
                do_sample=False,
                max_new_tokens=args.n_tokens,
                min_new_tokens=args.n_tokens,  # suppress EOS: mirrors ignore_eos
            )
        ids = out[0, inputs["input_ids"].shape[1] :].tolist()
        pins.append({"id": f"pin{index:02d}", "messages": messages, "expected_token_ids": ids})
        print(f"pin{index:02d}: {tokenizer.decode(ids)[:80]!r}", file=sys.stderr)

    payload = {
        "version": PINS_SCHEMA_VERSION,
        "model": args.model_path,
        "n_tokens": args.n_tokens,
        "enable_thinking": False,
        "pins": pins,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {len(pins)} pins to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
