"""
Accuracy checker: verify that the custom LLaMA model implementation
produces identical output to the HuggingFace transformers
AutoModelForCausalLM reference.

Both paths use greedy decoding (temperature=0) so outputs must match exactly.

Device is auto-detected: cuda:0 if available, else cpu (AWS Trainium boxes
have no CUDA, so the HF reference and the custom model both run on CPU in
BF16 here — this checks correctness, not speed).

Usage:
    python checker.py                      # auto device
    python checker.py --model-dir ../model
    python checker.py --device cpu         # force
"""

import argparse
import sys
import time
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer


def _resolve_device(requested: str | None) -> str:
    """Pick a runnable device. Trainium boxes have no CUDA, so fall back to CPU.

    The HF *reference* model has no Neuron build, so it runs on CPU here; the
    custom implementation is also exercised on the same device for an
    apples-to-apples greedy token-ID comparison (correctness, not speed —
    throughput is measured separately by the benchmark).
    """
    if requested and requested.lower() != "auto":
        return requested
    if torch.cuda.is_available():
        return "cuda:0"
    return "cpu"


def _dtype_for(device: str) -> torch.dtype:
    """BF16 on CPU/Neuron (matches the BF16 serving dtype and is well-supported
    on CPU, unlike FP16); FP16 on CUDA for parity with GPU serving."""
    return torch.float16 if device.startswith("cuda") else torch.bfloat16


def _load_custom_model_class():
    """Import VibeServeModel from main.py."""
    try:
        from main import VibeServeModel
    except ImportError as exc:
        raise RuntimeError(
            "Could not import VibeServeModel from main.py.\n"
            "Expected main.py to export:\n"
            "  class VibeServeModel with:\n"
            "    - VibeServeModel.from_pretrained(model_dir, device, dtype) -> model\n"
            "    - model.generate(input_ids, max_new_tokens=N) -> token_ids tensor\n"
        ) from exc
    return VibeServeModel


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
# Custom model: uses VibeServeModel.generate()
# ---------------------------------------------------------------------------

@torch.inference_mode()
def generate_custom(model, tokenizer, prompt_text, max_new_tokens, device):
    """Generate using the custom model's .generate() method."""
    inputs = tokenizer(prompt_text, return_tensors="pt").to(device)
    output_ids = model.generate(inputs.input_ids, max_new_tokens=max_new_tokens)
    prompt_len = inputs.input_ids.shape[1]
    return output_ids[0, prompt_len:].tolist()


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
    parser.add_argument(
        "--model-dir", type=str, default="../model",
        help="Local path to model weights directory (default: ../model)",
    )
    parser.add_argument(
        "--device", type=str, default="auto",
        help="Device for both reference and custom model. 'auto' (default) "
             "uses cuda:0 if available, else cpu (Trainium boxes have no CUDA).",
    )
    args = parser.parse_args()

    device = _resolve_device(args.device)
    model_dir = str(Path(args.model_dir).resolve())
    dtype = _dtype_for(device)
    print(f"Accuracy check device={device} dtype={dtype}")

    # --- Load tokenizer (from local path) ---
    print(f"Loading tokenizer from: {model_dir}")
    tokenizer = AutoTokenizer.from_pretrained(model_dir)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # --- Build test suite (before loading models to save GPU time) ---
    test_cases: list[tuple[str, int, str]] = list(RAW_COMPLETION_SAMPLES)

    has_chat_template = hasattr(tokenizer, "apply_chat_template") and tokenizer.chat_template
    for messages, max_tokens, desc in CHAT_SAMPLES:
        if has_chat_template:
            prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        else:
            prompt = "\n".join(f"{m['role']}: {m['content']}" for m in messages) + "\nassistant:"
        test_cases.append((prompt, max_tokens, f"[chat] {desc}"))

    # --- Generate reference outputs with HF model, then unload ---
    # Load on CPU first, then move with .to(device) — avoids device_map, which
    # requires `accelerate` and assumes CUDA.
    print(f"\nLoading HF reference model on {device} ...")
    t0 = time.perf_counter()
    ref_model = AutoModelForCausalLM.from_pretrained(
        model_dir, torch_dtype=dtype, attn_implementation="eager",
    )
    ref_model.to(device).eval()
    print(f"  HF model loaded in {time.perf_counter() - t0:.1f}s")

    print("Generating reference outputs ...")
    ref_outputs: list[list[int]] = []
    for prompt, max_tokens, desc in test_cases:
        ref_outputs.append(generate_reference(ref_model, tokenizer, prompt, max_tokens, device))

    # Unload reference model to free memory.
    del ref_model
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    print("  HF reference model unloaded.\n")

    # --- Load custom model ---
    print(f"Loading custom model (VibeServeModel from main.py) on {device} ...")
    t0 = time.perf_counter()
    VibeServeModel = _load_custom_model_class()
    custom_model = VibeServeModel.from_pretrained(model_dir, device, dtype)
    print(f"  Custom model loaded in {time.perf_counter() - t0:.1f}s\n")

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

        ref_ids = ref_outputs[i - 1]
        custom_ids = generate_custom(custom_model, tokenizer, prompt, max_tokens, device)

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
