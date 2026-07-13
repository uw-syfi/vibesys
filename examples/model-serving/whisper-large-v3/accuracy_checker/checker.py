"""Accuracy checker for the whisper-large-v3 offline ASR serving candidate.

Verifies the custom serving implementation transcribes consistently with the
HuggingFace `WhisperForConditionalGeneration` reference. The candidate must
expose, importable from `main.py` on `sys.path`:

    class VibeServeModel:
        @classmethod
        def from_pretrained(cls, model_dir, device, dtype) -> "VibeServeModel": ...
        def transcribe(self, audio: np.ndarray, sampling_rate: int = 16000) -> str: ...

Gate: for every test clip, the candidate transcript must either match the
reference exactly (after normalization) or reach a minimum word-overlap ratio
against it. Exit 0 iff every clip passes.

Usage:
    uv run python accuracy_checker/checker.py [--model-dir <dir>] [--threshold 0.9]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import soundfile as sf
import torch

# The read-only reference lives in the sibling reference/ directory.
sys.path.insert(0, str((Path(__file__).parent.parent / "reference").resolve()))
from reference import load_reference, reference_transcribe  # noqa: E402


def _load_custom_model_class():
    try:
        from main import VibeServeModel
    except ImportError as exc:
        raise RuntimeError(
            "Could not import VibeServeModel from main.py.\n"
            "Expected main.py to export a class VibeServeModel with:\n"
            "  - VibeServeModel.from_pretrained(model_dir, device, dtype) -> model\n"
            "  - model.transcribe(audio_array, sampling_rate=16000) -> str\n"
        ) from exc
    return VibeServeModel


def load_test_samples(audio_dir: Path):
    manifest_path = audio_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"manifest.json not found in {audio_dir}")
    with open(manifest_path) as f:
        manifest = json.load(f)
    samples = []
    for entry in manifest:
        arr, sr = sf.read(str(audio_dir / entry["file"]), dtype="float32")
        if arr.ndim > 1:
            arr = arr.mean(axis=1)
        desc = f"{entry['file']} ({entry['duration_s']:.1f}s)"
        samples.append((desc, arr, sr, entry.get("text", "")))
    return samples


def normalize_text(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def word_overlap_ratio(ref: str, hyp: str) -> float:
    rw, hw = set(normalize_text(ref).split()), set(normalize_text(hyp).split())
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
        f"  Reference: {ref_text[:100]!r}\n  Custom:    {custom_text[:100]!r}"
    )
    if overlap >= threshold:
        return True, f"PASS (word overlap {overlap:.1%} >= {threshold:.0%}):\n{detail}"
    return False, f"MISMATCH (word overlap {overlap:.1%} < {threshold:.0%}):\n{detail}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", type=str, default="../model")
    parser.add_argument("--audio-dir", type=str, default=None)
    parser.add_argument("--device", type=str, default="cuda:0")
    parser.add_argument("--threshold", type=float, default=0.9)
    args = parser.parse_args()

    model_dir = str(Path(args.model_dir).resolve())
    dtype = torch.float16
    device = args.device if torch.cuda.is_available() else "cpu"
    if device == "cpu":
        dtype = torch.float32

    audio_dir = (
        Path(args.audio_dir).resolve()
        if args.audio_dir
        else (Path(__file__).parent.parent / "test_audio").resolve()
    )
    print(f"Loading test audio from: {audio_dir}")
    test_samples = load_test_samples(audio_dir)
    print(f"  Loaded {len(test_samples)} samples\n")

    print(f"Loading HF reference (WhisperForConditionalGeneration) on {device} ...")
    t0 = time.perf_counter()
    ref_model, ref_proc = load_reference(model_dir, device, dtype)
    print(f"  HF model loaded in {time.perf_counter() - t0:.1f}s")
    print("Generating reference outputs ...")
    ref_outputs = [
        reference_transcribe(ref_model, ref_proc, audio, sr)
        for _desc, audio, sr, _ in test_samples
    ]
    del ref_model, ref_proc
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    print("  HF model unloaded.\n")

    print(f"Loading custom model (VibeServeModel) on {device} ...")
    t0 = time.perf_counter()
    custom_model = _load_custom_model_class().from_pretrained(model_dir, device, dtype)
    print(f"  Custom model loaded in {time.perf_counter() - t0:.1f}s\n")

    total, passed = len(test_samples), 0
    print("=" * 70)
    print(f"Running {total} test cases (threshold={args.threshold:.0%} word overlap)")
    print("=" * 70)
    for i, (desc, audio, sr, _gt) in enumerate(test_samples, 1):
        print(f"\n[{i}/{total}] {desc}")
        custom_text = custom_model.transcribe(audio, sampling_rate=sr).strip()
        ok, detail = compare_outputs(ref_outputs[i - 1], custom_text, args.threshold)
        print(("  PASS - " if ok else "  FAIL - ") + detail)
        passed += int(ok)

    print("\n" + "=" * 70)
    print(f"Results: {passed}/{total} passed, {total - passed}/{total} failed")
    print("=" * 70)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
