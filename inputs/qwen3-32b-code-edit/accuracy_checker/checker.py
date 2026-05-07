"""
Accuracy checker for the code-edit server.

Drives ``POST /v1/completions`` with code-debug rows from
``m-a-p/CodeEditorBench`` at temperature 0 and asserts a single loose
quality gate:

  **Gold-similarity rate**: fraction of steady samples whose output has
  ``SequenceMatcher.ratio()`` against the reference fixed solution at
  or above ``--min-gold-similarity`` (default 0.50) must be at or above
  ``--min-gold-rate`` (default 0.50). The threshold is intentionally
  loose — at ratio 0.50 the model has produced something that is at
  least half-aligned with the gold fix, which is enough to rule out
  servers that return prose, errors, or arbitrary unrelated text.

Exit code 0 iff every request returned a response AND the gate passes;
exit 1 otherwise.

The dataset seed is **random by default** so over-fitting to a fixed
slice isn't possible. Pass ``--seed <int>`` for reproducible runs.

Usage (server must already be running):

    python checker.py --url http://localhost:8000 --num-samples 10
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import random
import sys
import time
from typing import Any

import httpx


# ---------------------------------------------------------------------------
# Dataset loading (mirrors bench/benchmark.py)
# ---------------------------------------------------------------------------


def _load_codeeditorbench(
    languages: list[str],
    max_input_chars: int,
    num_samples: int,
    seed: int | None,
) -> list[dict]:
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise SystemExit(
            "The `datasets` library is required — pip install datasets."
        ) from exc

    # Pin to the code_debug shards — the only ones with both
    # `incorrect_solutions` and `solutions` columns. The other task
    # files have different schemas that don't unify.
    ds = load_dataset(
        "m-a-p/CodeEditorBench",
        data_files=["code_debug_primary.jsonl", "code_debug_plus.jsonl"],
        split="train",
        streaming=True,
    )
    rng = random.Random(seed)

    buffer: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for i, row in enumerate(ds):
        if "incorrect_solutions" not in row or "solutions" not in row:
            continue
        lang = row.get("code_language") or ""
        if languages and lang not in languages:
            continue
        inc = row.get("incorrect_solutions") or ""
        sol = row.get("solutions") or ""
        if not inc or not sol or inc.strip() == sol.strip():
            continue
        if len(inc) > max_input_chars:
            continue
        key = (inc.strip(), sol.strip())
        if key in seen:
            continue
        seen.add(key)
        buffer.append({
            "unique_id": str(row.get("idx") or i),
            "language": lang,
            "incorrect_code": inc,
            "gold_code": sol,
            "bug_type": row.get("type") or "",
        })
        if len(buffer) >= max(num_samples * 5, 500):
            break

    rng.shuffle(buffer)
    return buffer[:num_samples]


_SYSTEM_MESSAGE = (
    "You are a code-fixing assistant. The user gives you a buggy program "
    "and you reply with the corrected program — and ONLY the corrected "
    "program. No prose, no markdown fences, no explanation. Preserve all "
    "code that is not part of the fix exactly as given."
)


def _build_user_message(language: str, incorrect_code: str, bug_type: str) -> str:
    hint = f" (the bug is a {bug_type})" if bug_type else ""
    return (
        f"Fix the following {language} program{hint}. Return the corrected "
        f"program only.\n\n"
        f"```{language}\n{incorrect_code}\n```"
    )


def _load_tokenizer(tokenizer_path: str):
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise SystemExit(
            "The `transformers` library is required for client-side chat "
            "templating — pip install transformers."
        ) from exc
    return AutoTokenizer.from_pretrained(tokenizer_path)


def _build_prompt(tokenizer, sample: dict) -> str:
    messages = [
        {"role": "system", "content": _SYSTEM_MESSAGE},
        {
            "role": "user",
            "content": _build_user_message(
                sample["language"], sample["incorrect_code"], sample["bug_type"],
            ),
        },
    ]
    kwargs = dict(tokenize=False, add_generation_prompt=True)
    try:
        return tokenizer.apply_chat_template(
            messages, enable_thinking=False, **kwargs
        )
    except TypeError:
        return tokenizer.apply_chat_template(messages, **kwargs)


# ---------------------------------------------------------------------------
# Quality measurement
# ---------------------------------------------------------------------------


def _strip_code_fence(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
    if s.endswith("```"):
        s = s[: s.rfind("```")].rstrip()
    return s


def _evaluate(output_text: str, gold: str, min_gold_similarity: float) -> dict:
    out = _strip_code_fence(output_text)
    if not out:
        return {
            "ratio_to_gold": 0.0,
            "gold_similar": False,
            "reason": "empty-output",
        }
    r_gold = difflib.SequenceMatcher(None, out, gold, autojunk=False).ratio()
    gold_similar = r_gold >= min_gold_similarity
    reason = "ok" if gold_similar else f"gold-similarity {r_gold:.3f} < {min_gold_similarity:.3f}"
    return {
        "ratio_to_gold": r_gold,
        "gold_similar": gold_similar,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Send one request
# ---------------------------------------------------------------------------


async def _send(
    client: httpx.AsyncClient,
    url: str,
    prompt: str,
    prediction_content: str,
    max_tokens: int,
    temperature: float,
    request_timeout: float,
) -> dict:
    body = {
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "prediction": {
            "type": "content",
            "content": prediction_content,
        },
        "prompt_is_preformatted": True,
    }
    t_send = time.perf_counter()
    parts: list[str] = []
    error: str | None = None
    try:
        async with client.stream(
            "POST", url, json=body, timeout=request_timeout,
        ) as resp:
            resp.raise_for_status()
            async for raw in resp.aiter_lines():
                if not raw.startswith("data: "):
                    continue
                payload = raw[len("data: "):]
                if payload.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choice = chunk["choices"][0]
                text = choice.get("text") or ""
                if text:
                    parts.append(text)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    return {
        "output_text": "".join(parts),
        "error": error,
        "elapsed_s": time.perf_counter() - t_send,
    }


# ---------------------------------------------------------------------------
# Main driver
# ---------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    url = args.url.rstrip("/") + args.endpoint
    languages = [s.strip() for s in args.languages.split(",") if s.strip()]
    samples = _load_codeeditorbench(
        languages, args.max_input_chars, args.num_samples, args.seed,
    )
    if not samples:
        print("ERROR: no samples loaded from CodeEditorBench.", file=sys.stderr)
        return 1

    warmup_n = max(0, min(args.warmup, args.num_samples - 1))
    print(f"Loading tokenizer for client-side chat templating: {args.tokenizer_path}")
    tokenizer = _load_tokenizer(args.tokenizer_path)

    print(
        f"Hitting {url} with {len(samples)} samples "
        f"(languages={languages}, seed={args.seed}, warmup={warmup_n}, "
        f"max_tokens={args.max_tokens}, temperature={args.temperature})",
    )

    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient() as client:
        for idx, sample in enumerate(samples):
            prompt = _build_prompt(tokenizer, sample)
            r = await _send(
                client, url, prompt,
                prediction_content=sample["incorrect_code"],
                max_tokens=args.max_tokens,
                temperature=args.temperature,
                request_timeout=args.request_timeout,
            )
            r["sample_id"] = sample["unique_id"]
            r["language"] = sample["language"]
            r["is_warmup"] = idx < warmup_n
            if r["error"] is None:
                eval_ = _evaluate(
                    r["output_text"], sample["gold_code"], args.min_gold_similarity,
                )
            else:
                eval_ = {
                    "ratio_to_gold": 0.0,
                    "gold_similar": False,
                    "reason": f"request-error: {r['error']}",
                }
            r.update(eval_)
            results.append(r)

            tag = "WARMUP " if r["is_warmup"] else ""
            status = "OK  " if eval_["gold_similar"] else "FAIL"
            preview = r["output_text"].strip().replace("\n", " ")[:80]
            print(
                f"  [{idx + 1:02d}/{len(samples)}] {tag}{status} "
                f"id={sample['unique_id']} lang={sample['language']} "
                f"elapsed={r['elapsed_s']:.2f}s "
                f"r2gold={eval_['ratio_to_gold']:.3f} "
                f"reason={eval_['reason']} "
                f"out={preview!r}"
            )

    steady = [r for r in results if not r["is_warmup"]]
    errored = [r for r in steady if r["error"] is not None]
    gold_ok = [r for r in steady if r["gold_similar"]]
    gold_rate = len(gold_ok) / max(1, len(steady))

    print()
    print("=" * 60)
    print("  Code-edit accuracy check")
    print("=" * 60)
    print(f"Samples (steady):    {len(steady)} (warmup discarded: {warmup_n})")
    print(f"Request errors:      {len(errored)}/{len(steady)}")
    print(f"Gold-similar:        {len(gold_ok)}/{len(steady)} ({gold_rate:.1%})   "
          f"[min {args.min_gold_rate:.1%} at threshold {args.min_gold_similarity:.2f}]")

    passed = not errored and gold_rate >= args.min_gold_rate

    if errored:
        print("\nFirst failing requests (transport):")
        for r in errored[:5]:
            print(f"  - id={r['sample_id']}  {r['reason']}")

    failing = [r for r in steady if r["error"] is None and not r["gold_similar"]]
    if failing:
        print("\nFirst below-threshold outputs:")
        for r in failing[:5]:
            preview = r["output_text"].strip().replace("\n", " ")[:120]
            print(
                f"  - id={r['sample_id']}  {r['reason']}  "
                f"r2gold={r['ratio_to_gold']:.3f}  out={preview!r}"
            )

    if args.output_json:
        summary = {
            "url": url,
            "languages": languages,
            "num_samples": args.num_samples,
            "warmup": warmup_n,
            "num_steady": len(steady),
            "num_errors": len(errored),
            "num_gold_similar": len(gold_ok),
            "gold_rate": gold_rate,
            "min_gold_rate": args.min_gold_rate,
            "min_gold_similarity": args.min_gold_similarity,
            "passed": passed,
            "results": results,
        }
        from pathlib import Path
        Path(args.output_json).write_text(json.dumps(summary, indent=2))
        print(f"\nWrote detailed results to {args.output_json}")

    print()
    print("ACCURACY CHECK PASSED" if passed else "ACCURACY CHECK FAILED")
    return 0 if passed else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Code-edit accuracy checker. Hits a running server's "
            "/v1/completions endpoint with CodeEditorBench code-debug rows "
            "and asserts the output's SequenceMatcher ratio against the "
            "gold fix is at or above a loose threshold."
        ),
    )
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--endpoint", default="/v1/completions")
    parser.add_argument("--tokenizer-path", default="Qwen/Qwen3-32B")
    parser.add_argument("--languages", default="python3")
    parser.add_argument("--max-input-chars", type=int, default=4000)
    parser.add_argument("--num-samples", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--temperature", type=float, default=0)
    parser.add_argument(
        "--seed",
        type=lambda s: None if s.lower() in ("none", "random") else int(s),
        default=None,
    )
    parser.add_argument(
        "--min-gold-rate",
        type=float,
        default=0.50,
        help="Min fraction of samples whose output meets --min-gold-similarity.",
    )
    parser.add_argument(
        "--min-gold-similarity",
        type=float,
        default=0.50,
        help="Per-sample SequenceMatcher.ratio() threshold against gold solution.",
    )
    parser.add_argument("--request-timeout", type=float, default=600.0)
    parser.add_argument("--output-json", type=str, default=None)
    args = parser.parse_args()

    if args.warmup >= args.num_samples:
        args.warmup = max(0, args.num_samples - 1)

    rc = asyncio.run(run(args))
    sys.exit(rc)


if __name__ == "__main__":
    main()
