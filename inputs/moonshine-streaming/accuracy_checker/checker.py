"""
Accuracy checker: verify that the custom moonshine-streaming-medium serving
implementation produces transcriptions consistent with the HuggingFace
`MoonshineStreamingModel` reference.

The custom side is expected to expose:

    class VibeServeModel:
        @classmethod
        def from_pretrained(cls, model_dir, device, dtype) -> "VibeServeModel": ...
        def transcribe(self, audio: np.ndarray, sampling_rate: int = 16000) -> str: ...

importable from `main.py` on `sys.path`.

Usage:
    .venv/bin/python checker.py --model-dir <local model dir>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch


def _load_custom_model_class():
    try:
        from main import VibeServeModel
    except ImportError as exc:
        raise RuntimeError(
            "Could not import VibeServeModel from main.py.\n"
            "Expected main.py to export:\n"
            "  class VibeServeModel with:\n"
            "    - VibeServeModel.from_pretrained(model_dir, device, dtype) -> model\n"
            "    - model.transcribe(audio_array, sampling_rate=16000) -> str\n"
        ) from exc
    return VibeServeModel


# ---------- audio loading ----------

def load_test_samples(audio_dir: Path):
    manifest_path = audio_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"manifest.json not found in {audio_dir}")
    with open(manifest_path) as f:
        manifest = json.load(f)
    samples = []
    for entry in manifest:
        wav_path = audio_dir / entry["file"]
        arr, sr = sf.read(str(wav_path), dtype="float32")
        if arr.ndim > 1:
            arr = arr.mean(axis=1)
        desc = f"{entry['file']} ({entry['duration_s']:.1f}s)"
        samples.append((desc, arr, sr, entry["text"]))
    return samples


# ---------- HF reference ----------

@torch.inference_mode()
def transcribe_reference(model, proj_out, tokenizer, audio: np.ndarray, sr: int) -> str:
    """Greedy decode with HF MoonshineStreamingModel + a separately-loaded
    proj_out (lm_head) projection.  Matches the offline-transcribe contract."""
    from transformers.cache_utils import DynamicCache, EncoderDecoderCache
    device = next(model.parameters()).device
    dtype = next(model.parameters()).dtype
    audio_t = torch.from_numpy(audio).to(device).to(dtype).unsqueeze(0)
    am = torch.ones_like(audio_t, dtype=torch.long)
    enc = model.encoder(audio_t, attention_mask=am)
    enc_h = enc.last_hidden_state
    cur = torch.tensor([[model.config.decoder_start_token_id]], device=device, dtype=torch.long)
    out: list[int] = []
    pkv = None
    for _ in range(256):
        d = model.decoder(input_ids=cur, encoder_hidden_states=enc_h,
                          past_key_values=pkv, use_cache=True)
        nxt = int(proj_out(d.last_hidden_state)[0, -1].argmax().item())
        if nxt == model.config.eos_token_id:
            break
        out.append(nxt)
        cur = torch.tensor([[nxt]], device=device, dtype=torch.long)
        pkv = d.past_key_values
    return tokenizer.decode(out, skip_special_tokens=True).strip()


# ---------- text comparison ----------

def normalize_text(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def word_overlap_ratio(ref: str, hyp: str) -> float:
    rw = set(normalize_text(ref).split())
    hw = set(normalize_text(hyp).split())
    if not rw and not hw:
        return 1.0
    if not rw or not hw:
        return 0.0
    return len(rw & hw) / len(rw | hw)


def compare_outputs(ref_text: str, custom_text: str, threshold: float):
    if normalize_text(ref_text) == normalize_text(custom_text):
        return True, f"EXACT match: {ref_text[:80]!r}"
    overlap = word_overlap_ratio(ref_text, custom_text)
    detail = (
        f"  Reference: {ref_text[:100]!r}\n"
        f"  Custom:    {custom_text[:100]!r}"
    )
    if overlap >= threshold:
        return True, f"PASS (word overlap {overlap:.1%} >= {threshold:.0%}):\n{detail}"
    return False, f"MISMATCH (word overlap {overlap:.1%} < {threshold:.0%}):\n{detail}"


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", type=str, default="../model",
                        help="Local path to model weights directory")
    parser.add_argument("--audio-dir", type=str, default=None,
                        help="Directory with test audio + manifest.json (default: ../test_audio)")
    parser.add_argument("--device", type=str, default="cuda:0")
    parser.add_argument("--threshold", type=float, default=0.7,
                        help="Minimum word overlap ratio to pass")
    args = parser.parse_args()

    model_dir = str(Path(args.model_dir).resolve())
    dtype = torch.float16

    if args.audio_dir:
        audio_dir = Path(args.audio_dir).resolve()
    else:
        audio_dir = (Path(__file__).parent.parent / "test_audio").resolve()

    print(f"Loading test audio from: {audio_dir}")
    test_samples = load_test_samples(audio_dir)
    print(f"  Loaded {len(test_samples)} samples\n")

    # ---- HF reference ----
    print(f"Loading HF reference (MoonshineStreamingModel) on {args.device} ...")
    from transformers import AutoModel
    from safetensors.torch import load_file
    from tokenizers import Tokenizer
    t0 = time.perf_counter()
    ref_model = AutoModel.from_pretrained(model_dir, torch_dtype=dtype, device_map=args.device).eval()
    # proj_out (lm_head) is part of MoonshineStreamingForConditionalGeneration; AutoModel
    # loads the base MoonshineStreamingModel without it.  Load the weight separately.
    try:
        sd = load_file(str(Path(model_dir) / "model.safetensors"))
    except FileNotFoundError:
        # fall back to .bin
        sd = torch.load(str(Path(model_dir) / "pytorch_model.bin"), map_location="cpu")
    proj_out = torch.nn.Linear(ref_model.config.hidden_size, ref_model.config.vocab_size, bias=False)
    proj_out.weight.data = sd["proj_out.weight"]
    proj_out = proj_out.to(args.device).to(dtype)
    tokenizer = Tokenizer.from_file(str(Path(model_dir) / "tokenizer.json"))
    print(f"  HF model loaded in {time.perf_counter() - t0:.1f}s")

    print("Generating reference outputs ...")
    ref_outputs: list[str] = []
    for desc, audio, sr, _ in test_samples:
        ref_outputs.append(transcribe_reference(ref_model, proj_out, tokenizer, audio, sr))

    del ref_model, proj_out, sd
    torch.cuda.empty_cache()
    print("  HF model unloaded.\n")

    # ---- custom model ----
    print(f"Loading custom model (VibeServeModel) on {args.device} ...")
    t0 = time.perf_counter()
    VibeServeModel = _load_custom_model_class()
    custom_model = VibeServeModel.from_pretrained(model_dir, args.device, dtype)
    print(f"  Custom model loaded in {time.perf_counter() - t0:.1f}s\n")

    # ---- run tests ----
    total = len(test_samples)
    passed = 0
    print("=" * 70)
    print(f"Running {total} test cases (threshold={args.threshold:.0%} word overlap)")
    print("=" * 70)

    for i, (desc, audio, sr, _gt) in enumerate(test_samples, 1):
        print(f"\n[{i}/{total}] {desc}")
        ref_text = ref_outputs[i - 1]
        custom_text = custom_model.transcribe(audio, sampling_rate=sr).strip()
        ok, detail = compare_outputs(ref_text, custom_text, args.threshold)
        print(("  PASS - " if ok else "  FAIL - ") + detail)
        if ok:
            passed += 1

    print("\n" + "=" * 70)
    print(f"Results: {passed}/{total} passed, {total - passed}/{total} failed")
    print("=" * 70)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
