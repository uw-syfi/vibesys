"""Generate `golden.json` with HF transformers on the GPU (run once; the output is checked in).

    python -m accuracy_checker.make_golden --model Qwen/Qwen3.5-9B [--no-fla] [--out PATH]

For every prompt in `prompts.CASES`: greedy-decode `max_new_tokens` tokens with
EOS ignored, then run one teacher-forced forward over prompt + continuation and
record, per continuation position, HF's logprob of the golden token, its top-1
id, and its top-1 minus top-2 logprob margin (the near-tie measure the gate
uses).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .prompts import CASES, PromptCase
from .targets import HFTarget

DEFAULT_OUT = Path(__file__).with_name("golden.json")


def encode(tokenizer, case: PromptCase) -> list[int]:
    if case.kind == "chat":
        text = tokenizer.apply_chat_template(
            [{"role": "user", "content": case.text}],
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )
    else:
        text = case.text
    return tokenizer(text, add_special_tokens=False).input_ids


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--model", default="Qwen/Qwen3.5-9B")
    p.add_argument(
        "--no-fla", action="store_true", help="use HF's torch GDN fallback instead of fla kernels"
    )
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = p.parse_args()

    hf = HFTarget(args.model, use_fla=not args.no_fla)  # must precede any other torch/fla import
    import torch
    import transformers
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(args.model)
    cases = []
    for case in CASES:
        t0 = time.perf_counter()
        prompt = encode(tok, case)
        cont = hf.greedy(prompt, case.max_new_tokens)
        lps = hf.forced_logprobs(prompt, cont)
        top2 = lps.topk(2, dim=-1)
        given = lps.gather(1, torch.tensor(cont, device=lps.device)[:, None])[:, 0]
        cases.append(
            {
                "name": case.name,
                "kind": case.kind,
                "prompt_ids": prompt,
                "greedy_ids": cont,
                "tf_logprob": [round(x, 5) for x in given.tolist()],
                "tf_top1": top2.indices[:, 0].tolist(),
                "tf_margin": [
                    round(x, 5) for x in (top2.values[:, 0] - top2.values[:, 1]).tolist()
                ],
            }
        )
        print(
            f"{case.name}: prompt={len(prompt)} tokens, {time.perf_counter() - t0:.1f}s | {tok.decode(cont)[:80]!r}"
        )
    meta = {
        "model": args.model,
        "source": hf.name,
        "dtype": "bfloat16",
        "transformers": transformers.__version__,
        "torch": torch.__version__,
        "device": torch.cuda.get_device_name(0),
        "greedy": "argmax loop over HF cache, EOS ignored",
    }
    args.out.write_text(json.dumps({"meta": meta, "cases": cases}, separators=(",", ":")) + "\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
