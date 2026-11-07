"""Regenerate `references.json`, the transcripts the accuracy checker compares against.

The checked-in file is the source of truth; rerun this only when the test clips or the
pinned model revision change. It runs the Hugging Face reference path the checker ran
in-process before it became service-style: `MoonshineStreamingModel` with the
separately loaded `proj_out` head, greedy decode of at most 256 tokens, on every clip
in `../test_audio/manifest.json`, at the model revision pinned in
`../reference/meta.json`.

    uv run --no-project --python 3.12 \
        --with torch==2.9.1 --with transformers==5.7.0 \
        --with safetensors --with tokenizers --with soundfile --with numpy \
        python make_references.py [--device cpu] [--dtype float32] [--offline]

The checker's comparison tolerates the small differences between devices and dtypes,
so any device works; the file records which one produced it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import transformers
from huggingface_hub import snapshot_download
from safetensors.torch import load_file
from tokenizers import Tokenizer
from transformers import AutoModel

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
MAX_NEW_TOKENS = 256


@torch.inference_mode()
def transcribe_reference(model, proj_out, tokenizer, audio: np.ndarray) -> str:
    """Greedy decode with HF MoonshineStreamingModel + the separately loaded proj_out."""
    device = next(model.parameters()).device
    dtype = next(model.parameters()).dtype
    audio_t = torch.from_numpy(audio).to(device).to(dtype).unsqueeze(0)
    am = torch.ones_like(audio_t, dtype=torch.long)
    enc_h = model.encoder(audio_t, attention_mask=am).last_hidden_state
    cur = torch.tensor([[model.config.decoder_start_token_id]], device=device, dtype=torch.long)
    out: list[int] = []
    pkv = None
    for _ in range(MAX_NEW_TOKENS):
        d = model.decoder(
            input_ids=cur, encoder_hidden_states=enc_h, past_key_values=pkv, use_cache=True
        )
        nxt = int(proj_out(d.last_hidden_state)[0, -1].argmax().item())
        if nxt == model.config.eos_token_id:
            break
        out.append(nxt)
        cur = torch.tensor([[nxt]], device=device, dtype=torch.long)
        pkv = d.past_key_values
    return tokenizer.decode(out, skip_special_tokens=True).strip()


def main() -> None:
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
    model_dir = Path(
        snapshot_download(
            meta["model_id"], revision=meta["revision"], local_files_only=args.offline
        )
    )
    dtype = getattr(torch, args.dtype)

    model = AutoModel.from_pretrained(model_dir, torch_dtype=dtype).to(args.device).eval()
    # proj_out (lm_head) belongs to MoonshineStreamingForConditionalGeneration; AutoModel
    # loads the base MoonshineStreamingModel without it.
    weights = load_file(str(model_dir / "model.safetensors"))
    proj_out = torch.nn.Linear(model.config.hidden_size, model.config.vocab_size, bias=False)
    proj_out.weight.data = weights["proj_out.weight"]
    proj_out = proj_out.to(args.device).to(dtype)
    tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))

    clips = []
    for entry in json.loads((args.audio_dir / "manifest.json").read_text()):
        path = args.audio_dir / entry["file"]
        audio, _sr = sf.read(str(path), dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        text = transcribe_reference(model, proj_out, tokenizer, audio)
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
            "source": "make_references.py: HF MoonshineStreamingModel + proj_out, greedy, "
            f"at most {MAX_NEW_TOKENS} tokens",
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
