"""Regenerate `references.json`, the transcripts the accuracy checker compares against.

The checked-in file is the source of truth; rerun this only when the test clips or the
pinned model revision change. It runs the Hugging Face reference the checker ran
in-process before it became service-style, `reference_transcribe` in
`../reference/reference.py` (greedy, `<|en|><|transcribe|><|notimestamps|>`), on every
clip in `../test_audio/manifest.json`, at the model revision pinned in
`../reference/meta.json`.

    uv run --no-project --python 3.12 \
        --with torch==2.9.1 --with transformers==5.7.0 --with soundfile --with numpy \
        python make_references.py [--device cpu] [--dtype float32] [--offline]

The checker's comparison tolerates the small differences between devices and dtypes,
so any device works; the file records which one produced it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import soundfile as sf
import torch
import transformers
from huggingface_hub import snapshot_download

HERE = Path(__file__).resolve().parent
TASK = HERE.parent


def main() -> None:
    # The reference module lives in the sibling, read-only reference/ directory.
    sys.path.insert(0, str(TASK / "reference"))
    from reference import load_reference, reference_transcribe

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--dtype", choices=["float32", "float16", "bfloat16"], default="float32")
    parser.add_argument("--audio-dir", type=Path, default=TASK / "test_audio")
    parser.add_argument("--out", type=Path, default=HERE / "references.json")
    parser.add_argument(
        "--offline", action="store_true", help="use the pinned snapshot from the local HF cache"
    )
    args = parser.parse_args()

    meta = json.loads((TASK / "reference" / "meta.json").read_text())
    model_dir = snapshot_download(
        meta["model_id"], revision=meta["revision"], local_files_only=args.offline
    )
    model, processor = load_reference(model_dir, args.device, getattr(torch, args.dtype))

    clips = []
    for entry in json.loads((args.audio_dir / "manifest.json").read_text()):
        path = args.audio_dir / entry["file"]
        audio, sr = sf.read(str(path), dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        text = reference_transcribe(model, processor, audio, sr)
        print(f"{entry['file']}: {text!r}")
        clips.append(
            {
                "file": entry["file"],
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "text": text,
            }
        )

    document = {
        "meta": {
            "model_id": meta["model_id"],
            "revision": meta["revision"],
            "source": "make_references.py: reference/reference.py reference_transcribe "
            "(HF WhisperForConditionalGeneration.generate, greedy)",
            "device": str(torch.device(args.device)),
            "dtype": args.dtype,
            "torch": torch.__version__,
            "transformers": transformers.__version__,
        },
        "clips": clips,
    }
    args.out.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
