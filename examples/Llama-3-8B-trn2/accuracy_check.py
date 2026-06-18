"""
Accuracy checker: verify that the custom LLaMA model implementation
(step_1_fastapi.py) produces identical output to the HuggingFace
transformers AutoModelForCausalLM reference.

Both paths use greedy decoding (temperature=0) so outputs must match exactly.

Usage:
    CUDA_VISIBLE_DEVICES=0 .venv/bin/python accuracy_checker.py
    CUDA_VISIBLE_DEVICES=0 .venv/bin/python accuracy_checker.py --model meta-llama/Llama-3.1-8B-Instruct
"""

import argparse
import os
import sys
import time
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

from step_1_fastapi import load_llama_model


# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------

def _load_env():
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


_load_env()


# ---------------------------------------------------------------------------
# Test samples — diverse prompts covering various scenarios
# ---------------------------------------------------------------------------

RAW_COMPLETION_SAMPLES: list[tuple[str, int, str]] = [
    ("The capital of France is", 15, "short factual completion"),
    ("Once upon a time, in a land far away,", 50, "story continuation"),
    ("def fibonacci(n):\n    \"\"\"Return the n-th Fibonacci number.\"\"\"\n", 40, "code completion"),
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
]

CHAT_SAMPLES: list[tuple[list[dict[str, str]], int, str]] = [
    (
        [{"role": "user", "content": "What is 2+2? Answer in one word."}],
        10,
        "simple math chat",
    ),
    (
        [{"role": "user", "content": "Write a haiku about programming."}],
        40,
        "creative chat",
    ),
    (
        [
            {"role": "user", "content": "What is Python?"},
            {"role": "assistant", "content": "Python is a high-level programming language."},
            {"role": "user", "content": "What are its main features?"},
        ],
        40,
        "multi-turn chat",
    ),
    (
        [{"role": "user", "content": "Translate to French: 'Good morning, how are you?'"}],
        25,
        "translation chat",
    ),
]


# ---------------------------------------------------------------------------
# Reference: HuggingFace model.generate()
# ---------------------------------------------------------------------------

@torch.inference_mode()
def generate_reference(model, tokenizer, prompt_text, max_new_tokens, device):
    inputs = tokenizer(prompt_text, return_tensors="pt").to(device)
    prompt_len = inputs.input_ids.shape[1]
    output_ids = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
    return output_ids[0, prompt_len:].tolist()


# ---------------------------------------------------------------------------
# Our implementation: custom model with manual token-by-token loop
# ---------------------------------------------------------------------------

@torch.inference_mode()
def generate_custom(model, tokenizer, prompt_text, max_new_tokens, device):
    """Token-by-token loop using our custom LlamaForCausalLM."""
    inputs = tokenizer(prompt_text, return_tensors="pt").to(device)
    eos_id = tokenizer.eos_token_id

    generated_ids = inputs.input_ids
    past_key_values = None
    new_token_ids: list[int] = []

    for _ in range(max_new_tokens):
        model_input = generated_ids if past_key_values is None else generated_ids[:, -1:]
        logits, past_key_values = model(model_input, past_key_values)
        next_logits = logits[:, -1, :]

        next_token = next_logits.argmax(dim=-1, keepdim=True)
        token_id = next_token.item()

        if token_id == eos_id:
            new_token_ids.append(token_id)
            break

        generated_ids = torch.cat([generated_ids, next_token], dim=-1)
        new_token_ids.append(token_id)

    return new_token_ids


# ---------------------------------------------------------------------------
# Comparison logic
# ---------------------------------------------------------------------------

def compare_outputs(ref_ids, custom_ids, tokenizer):
    ref_text = tokenizer.decode(ref_ids, skip_special_tokens=True)
    custom_text = tokenizer.decode(custom_ids, skip_special_tokens=True)

    if ref_ids == custom_ids:
        return True, f"EXACT match ({len(ref_ids)} tokens): {ref_text!r}"

    min_len = min(len(ref_ids), len(custom_ids))
    first_diff = min_len
    for i in range(min_len):
        if ref_ids[i] != custom_ids[i]:
            first_diff = i
            break

    detail = (
        f"MISMATCH at token {first_diff}.\n"
        f"  Reference ({len(ref_ids):3d} tokens): {ref_text!r}\n"
        f"  Custom    ({len(custom_ids):3d} tokens): {custom_text!r}\n"
        f"  Ref  ids[{first_diff}:]: {ref_ids[first_diff:first_diff+10]}\n"
        f"  Cust ids[{first_diff}:]: {custom_ids[first_diff:first_diff+10]}"
    )
    return False, detail


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Accuracy checker: custom model vs HF reference")
    parser.add_argument("--model", type=str, default="meta-llama/Llama-3.1-8B-Instruct")
    parser.add_argument("--device", type=str, default="cuda:0")
    args = parser.parse_args()

    hf_token = os.environ.get("HF_TOKEN")
    dtype = torch.float16

    # --- Load tokenizer ---
    print(f"Loading tokenizer: {args.model}")
    tokenizer = AutoTokenizer.from_pretrained(args.model, token=hf_token)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # --- Load HF reference model ---
    print(f"Loading HF reference model on {args.device} ...")
    t0 = time.perf_counter()
    ref_model = AutoModelForCausalLM.from_pretrained(
        args.model, torch_dtype=dtype, device_map=args.device, token=hf_token,
        attn_implementation="eager",
    )
    ref_model.eval()
    print(f"  HF model loaded in {time.perf_counter() - t0:.1f}s")

    # --- Load our custom model ---
    print(f"Loading custom model on {args.device} ...")
    t0 = time.perf_counter()
    custom_model, _ = load_llama_model(args.model, args.device, dtype, hf_token)
    print(f"  Custom model loaded in {time.perf_counter() - t0:.1f}s\n")

    # --- Build test suite ---
    test_cases: list[tuple[str, int, str]] = list(RAW_COMPLETION_SAMPLES)

    has_chat_template = hasattr(tokenizer, "apply_chat_template") and tokenizer.chat_template
    for messages, max_tokens, desc in CHAT_SAMPLES:
        if has_chat_template:
            prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        else:
            prompt = "\n".join(f"{m['role']}: {m['content']}" for m in messages) + "\nassistant:"
        test_cases.append((prompt, max_tokens, f"[chat] {desc}"))

    # --- Run all tests ---
    passed = 0
    failed = 0
    total = len(test_cases)

    print("=" * 70)
    print(f"Running {total} test cases (greedy decoding)")
    print("=" * 70)

    for i, (prompt, max_tokens, desc) in enumerate(test_cases, 1):
        print(f"\n[{i}/{total}] {desc}  (max_tokens={max_tokens})")
        prompt_preview = prompt[:80].replace("\n", "\\n")
        if len(prompt) > 80:
            prompt_preview += "..."
        print(f"  Prompt: {prompt_preview!r}")

        ref_ids = generate_reference(ref_model, tokenizer, prompt, max_tokens, args.device)
        custom_ids = generate_custom(custom_model, tokenizer, prompt, max_tokens, args.device)

        match, detail = compare_outputs(ref_ids, custom_ids, tokenizer)
        if match:
            print(f"  PASS - {detail}")
            passed += 1
        else:
            print(f"  FAIL - {detail}")
            failed += 1

    # --- Summary ---
    print("\n" + "=" * 70)
    print(f"Results: {passed}/{total} passed, {failed}/{total} failed")
    print("=" * 70)

    if failed > 0:
        print("\nACCURACY CHECK FAILED")
        sys.exit(1)
    else:
        print("\nALL CHECKS PASSED")
        sys.exit(0)


if __name__ == "__main__":
    main()
