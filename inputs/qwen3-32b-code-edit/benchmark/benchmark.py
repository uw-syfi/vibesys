"""
Single-batch code-edit latency benchmark for predicted-outputs headroom.

Drives an OpenAI-compatible ``/v1/completions`` server with code-debug
samples drawn from ``m-a-p/CodeEditorBench``. Each request is sent
**sequentially** (no concurrency) — the goal is to measure *single-batch*
latency, which is what matters when you're optimizing speculative
decoding for an interactive code-edit endpoint.

Each request body carries the OpenAI predicted-outputs envelope
(``prediction.content``) **in addition to** ``prompt``. Servers that
don't implement predicted outputs (vLLM, SGLang today) ignore the
field and the request still parses as a normal completion; a server
that consumes the field is the configuration this benchmark scores.

What the server MUST implement to score well here:

1. ``POST /v1/completions`` accepting the usual OpenAI body plus an
   optional ``prediction`` field of shape::

       {"type": "content", "content": "<predicted output text>"}

2. Streaming SSE response with ``choices[0].text`` deltas, terminated
   by ``data: [DONE]``.

Token count is canonicalised by re-tokenising the concatenated server
output. This keeps tok/s independent of how the server batches accepted
spec tokens into SSE chunks (vLLM/SGLang flush all accepted tokens in
one chunk, which would otherwise undercount).

Usage:
    python benchmark.py --url http://localhost:8000 --num-samples 50 \\
        --max-tokens 512 --output-json /tmp/code_edit.json
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import math
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------


def _load_codeeditorbench(
    languages: list[str],
    max_input_chars: int,
    num_samples: int,
    seed: int,
) -> list[dict]:
    """Load ``num_samples`` code-debug rows from m-a-p/CodeEditorBench.

    Returns a list of dicts with keys:
        - ``unique_id`` (str)
        - ``language`` (str)  -- one of ``python3``, ``cpp``, ``java``
        - ``incorrect_code`` (str)  -- the prediction
        - ``gold_code`` (str)       -- the reference fixed solution
        - ``bug_type`` (str)
        - ``difficulty`` (str)
    """
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise SystemExit(
            "The ``datasets`` library is required — pip install datasets."
        ) from exc

    # The dataset on the hub has four task files at the repo root; their
    # schemas don't unify, so the default loader fails. Pin to the
    # code_debug shards — those are the rows with both
    # `incorrect_solutions` and `solutions` columns.
    ds = load_dataset(
        "m-a-p/CodeEditorBench",
        data_files=["code_debug_primary.jsonl", "code_debug_plus.jsonl"],
        split="train",
        streaming=True,
    )
    rng = random.Random(seed)

    buffer: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()
    for i, row in enumerate(ds):
        # Only the code-debug subset has both incorrect_solutions + solutions.
        if "incorrect_solutions" not in row or "solutions" not in row:
            continue
        lang = row.get("code_language") or ""
        if languages and lang not in languages:
            continue
        inc = row.get("incorrect_solutions") or ""
        sol = row.get("solutions") or ""
        if not inc or not sol:
            continue
        if len(inc) > max_input_chars:
            continue
        if inc.strip() == sol.strip():
            continue
        key = (inc.strip(), sol.strip())
        if key in seen_keys:
            continue
        seen_keys.add(key)
        buffer.append({
            "unique_id": str(row.get("idx") or i),
            "language": lang,
            "incorrect_code": inc,
            "gold_code": sol,
            "bug_type": row.get("type") or "",
            "difficulty": str(row.get("difficulty") or ""),
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
    """Load the model's tokenizer for client-side chat templating.

    Pinning the tokenizer here is required for cross-engine fairness:
    every engine must see byte-identical input tokens. If we let each
    server apply its own template, prompt content drifts per engine.
    """
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise SystemExit(
            "The `transformers` library is required for client-side chat "
            "templating — pip install transformers."
        ) from exc
    return AutoTokenizer.from_pretrained(tokenizer_path)


def _build_prompt(tokenizer, sample: dict) -> str:
    """Build the FULL chat-templated prompt the model will see.

    Done client-side so every inference engine receives byte-identical
    input. Servers should treat the result as raw text (no further
    templating). Qwen3 supports ``enable_thinking=False``; we always
    disable it so generated tokens go straight to code.
    """
    messages = [
        {"role": "system", "content": _SYSTEM_MESSAGE},
        {
            "role": "user",
            "content": _build_user_message(
                sample["language"],
                sample["incorrect_code"],
                sample["bug_type"],
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
# Per-request measurement
# ---------------------------------------------------------------------------


async def send_request(
    client: httpx.AsyncClient,
    url: str,
    prompt: str,
    prediction_content: str,
    max_tokens: int,
    temperature: float,
    tokenizer,
    model_name: str | None = None,
    print_stream: bool = False,
    sample_id: str | None = None,
) -> dict:
    """Send one code-edit completion request and measure latency.

    The request body carries OpenAI's ``prediction`` envelope alongside
    the standard fields. Servers that don't implement predicted outputs
    will simply ignore the extra field — no need for any feature
    negotiation.
    """
    body: dict[str, Any] = {
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "prediction": {
            "type": "content",
            "content": prediction_content,
        },
        # vLLM/SGLang don't auto-template when given raw `prompt`; this
        # flag is for servers that would otherwise apply a chat template.
        "prompt_is_preformatted": True,
    }
    if model_name:
        # vLLM's OpenAI-compat endpoint requires `model`; it's optional
        # for custom servers that have only one loaded model.
        body["model"] = model_name

    t_send = time.perf_counter()
    t_first_token = None
    t_done = None
    num_chunks = 0
    text_parts: list[str] = []
    finish_reason: str | None = None
    error: str | None = None

    if print_stream:
        header = f"sample={sample_id}" if sample_id else ""
        sys.stderr.write(f"\n===== >>> PROMPT {header} =====\n{prompt}\n")
        sys.stderr.write(f"===== >>> PREDICTION ({len(prediction_content)} chars) =====\n{prediction_content}\n")
        sys.stderr.write("===== <<< STREAM =====\n")
        sys.stderr.flush()

    try:
        async with client.stream("POST", url, json=body, timeout=600.0) as resp:
            resp.raise_for_status()
            async for raw_line in resp.aiter_lines():
                if not raw_line.startswith("data: "):
                    continue
                payload = raw_line[len("data: "):]
                if payload.strip() == "[DONE]":
                    t_done = time.perf_counter()
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choice = chunk["choices"][0]
                text = choice.get("text") or ""
                reason = choice.get("finish_reason")
                if reason is not None:
                    finish_reason = reason
                if text:
                    if t_first_token is None:
                        t_first_token = time.perf_counter()
                    num_chunks += 1
                    text_parts.append(text)
                    if print_stream:
                        sys.stderr.write(text)
                        sys.stderr.flush()
    except Exception as exc:
        if print_stream:
            sys.stderr.write(f"\n[stream error: {type(exc).__name__}: {exc}]\n")
            sys.stderr.flush()
        error = f"{type(exc).__name__}: {exc}"
        t_done = time.perf_counter()

    if print_stream:
        sys.stderr.write("\n===== <<< END =====\n")
        sys.stderr.flush()

    if t_done is None:
        t_done = time.perf_counter()

    output_text = "".join(text_parts)
    output_tokens = (
        len(tokenizer.encode(output_text, add_special_tokens=False))
        if output_text else 0
    )
    result: dict = {
        "error": error,
        "output_tokens": output_tokens,
        "num_chunks": num_chunks,
        "output_text": output_text,
        "finish_reason": finish_reason,
        "total_latency": t_done - t_send,
    }
    if t_first_token is not None:
        result["ttft"] = t_first_token - t_send
        if output_tokens > 1:
            result["tpot"] = (t_done - t_first_token) / (output_tokens - 1)
        else:
            result["tpot"] = None
    else:
        result["ttft"] = None
        result["tpot"] = None
    return result


# ---------------------------------------------------------------------------
# Per-sample diff stats: drives the headroom estimator downstream
# ---------------------------------------------------------------------------


def _strip_code_fence(text: str) -> str:
    """Heuristically strip a single Markdown code fence pair.

    Models occasionally re-wrap their output even when told not to. The
    diff math is much cleaner if we drop the fence so it doesn't show up
    as a "diverged token run" at the start and end.
    """
    s = text.strip()
    if s.startswith("```"):
        # drop first line
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
    if s.endswith("```"):
        s = s[: s.rfind("```")].rstrip()
    return s


def _token_align(
    tokenizer,
    prediction_text: str,
    output_text: str,
) -> dict:
    """Align the model's output against the prediction at TOKEN level.

    Returns a summary dict suitable for headroom math:
        - num_output_tokens
        - num_matched_tokens               (sum of matched-run lengths)
        - num_diverged_tokens              (output - matched)
        - matched_run_lengths              (list[int], each token-length of an equal block in the output)
        - longest_matched_run              (int)

    Token-level alignment is what the predicted-outputs verify path
    actually consumes. Char-level alignment is misleading because a
    single token boundary mismatch can split a "matched" string into
    two diverged token runs.
    """
    pred_ids = tokenizer.encode(prediction_text or "", add_special_tokens=False)
    out_ids = tokenizer.encode(_strip_code_fence(output_text or ""), add_special_tokens=False)
    sm = difflib.SequenceMatcher(a=pred_ids, b=out_ids, autojunk=False)
    matched_run_lengths: list[int] = []
    matched_tokens = 0
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag == "equal" and j2 > j1:
            matched_run_lengths.append(j2 - j1)
            matched_tokens += j2 - j1
    return {
        "num_output_tokens": len(out_ids),
        "num_matched_tokens": matched_tokens,
        "num_diverged_tokens": len(out_ids) - matched_tokens,
        "matched_run_lengths": matched_run_lengths,
        "longest_matched_run": max(matched_run_lengths) if matched_run_lengths else 0,
    }


def _quality_score(
    output_text: str,
    incorrect_text: str,
    gold_text: str,
) -> dict:
    """Cheap correctness signal: closeness to gold vs to incorrect input.

    A pure echo-the-prediction bypass scores 1.0 on ``ratio_to_input``
    and ~the original similarity on ``ratio_to_gold``, so
    ``improved_over_input`` distinguishes it from a real fix.
    """
    out = _strip_code_fence(output_text)
    return {
        "ratio_to_gold": difflib.SequenceMatcher(
            None, out, gold_text, autojunk=False
        ).ratio(),
        "ratio_to_input": difflib.SequenceMatcher(
            None, out, incorrect_text, autojunk=False
        ).ratio(),
        "equals_input_verbatim": out.strip() == incorrect_text.strip(),
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p / 100.0
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def _fmt_stats(values: list[float], unit: str = "ms", multiplier: float = 1000.0) -> str:
    if not values:
        return "  (no data)\n"
    s = sorted(values)
    return (
        f"  Mean:    {(sum(s) / len(s)) * multiplier:.1f} {unit}\n"
        f"  P50:     {_percentile(s, 50) * multiplier:.1f} {unit}\n"
        f"  P90:     {_percentile(s, 90) * multiplier:.1f} {unit}\n"
        f"  P99:     {_percentile(s, 99) * multiplier:.1f} {unit}\n"
    )


def _pct_block(sorted_vals: list[float]) -> dict | None:
    if not sorted_vals:
        return None
    return {
        "mean_ms": sum(sorted_vals) / len(sorted_vals) * 1000,
        "p50_ms": _percentile(sorted_vals, 50) * 1000,
        "p90_ms": _percentile(sorted_vals, 90) * 1000,
        "p95_ms": _percentile(sorted_vals, 95) * 1000,
        "p99_ms": _percentile(sorted_vals, 99) * 1000,
    }


# ---------------------------------------------------------------------------
# Main driver
# ---------------------------------------------------------------------------


async def run_benchmark(args: argparse.Namespace) -> dict:
    url = args.url.rstrip("/") + args.endpoint
    languages = [s.strip() for s in args.languages.split(",") if s.strip()]
    print(
        f"[bench] loading {args.num_samples} samples from m-a-p/CodeEditorBench "
        f"(languages={languages or 'any'}, max_input_chars={args.max_input_chars})",
        file=sys.stderr,
    )
    samples = _load_codeeditorbench(
        languages, args.max_input_chars, args.num_samples, args.seed,
    )
    if not samples:
        raise SystemExit("No samples could be loaded from CodeEditorBench.")

    print(f"[bench] sending {len(samples)} sequential requests to {url}", file=sys.stderr)
    print(
        f"[bench] loading tokenizer for client-side chat templating: {args.tokenizer_path}",
        file=sys.stderr,
    )
    tokenizer = _load_tokenizer(args.tokenizer_path)

    results: list[dict] = []
    async with httpx.AsyncClient() as client:
        t_start = time.perf_counter()

        for idx, sample in enumerate(samples):
            prompt = _build_prompt(tokenizer, sample)
            result = await send_request(
                client, url, prompt,
                prediction_content=sample["incorrect_code"],
                max_tokens=args.max_tokens,
                temperature=args.temperature,
                tokenizer=tokenizer,
                model_name=args.model,
                print_stream=args.print_stream,
                sample_id=sample["unique_id"],
            )
            result["sample_id"] = sample["unique_id"]
            result["language"] = sample["language"]
            result["bug_type"] = sample["bug_type"]
            result["is_warmup"] = idx < args.warmup
            if result["error"] is None:
                # Token-level alignment vs the prediction (the buggy code)
                # — this is the input the headroom estimator needs.
                align = _token_align(
                    tokenizer, sample["incorrect_code"], result["output_text"],
                )
                quality = _quality_score(
                    result["output_text"],
                    sample["incorrect_code"],
                    sample["gold_code"],
                )
                result["alignment"] = align
                result["quality"] = quality
            else:
                result["alignment"] = None
                result["quality"] = None
            results.append(result)

            align = result["alignment"]
            qual = result["quality"]
            print(
                f"[bench] sample {idx + 1}/{len(samples)} "
                f"id={sample['unique_id']} lang={sample['language']} "
                f"tokens={result['output_tokens']} "
                f"chunks={result['num_chunks']} "
                f"ttft={(result['ttft'] or 0) * 1000:.0f}ms "
                f"total={(result['total_latency']) * 1000:.0f}ms "
                + (
                    f"matched={align['num_matched_tokens']}/{align['num_output_tokens']} "
                    f"longest_run={align['longest_matched_run']} "
                    f"r2gold={qual['ratio_to_gold']:.3f} "
                    if align is not None else ""
                )
                + (f"err={result['error'][:80]}" if result["error"] else ""),
                file=sys.stderr,
            )

        t_end = time.perf_counter()

    wall_clock = t_end - t_start

    steady = [r for r in results if not r["is_warmup"]]
    successes = [r for r in steady if r["error"] is None]
    errors = [r for r in steady if r["error"] is not None]

    ttfts = [r["ttft"] for r in successes if r["ttft"] is not None]
    tpots = [r["tpot"] for r in successes if r["tpot"] is not None]
    latencies = [r["total_latency"] for r in successes]
    output_tokens = [r["output_tokens"] for r in successes]

    # Diff stats aggregated over successes (drives headroom math).
    all_matched_runs: list[int] = []
    matched_tokens_total = 0
    diverged_tokens_total = 0
    for r in successes:
        a = r["alignment"]
        if a is None:
            continue
        all_matched_runs.extend(a["matched_run_lengths"])
        matched_tokens_total += a["num_matched_tokens"]
        diverged_tokens_total += a["num_diverged_tokens"]
    aggregated_match_rate = (
        matched_tokens_total / max(1, matched_tokens_total + diverged_tokens_total)
    )
    sorted_runs = sorted(all_matched_runs)
    matched_run_pct = {
        "p50": _percentile(sorted_runs, 50) if sorted_runs else None,
        "p75": _percentile(sorted_runs, 75) if sorted_runs else None,
        "p90": _percentile(sorted_runs, 90) if sorted_runs else None,
        "p95": _percentile(sorted_runs, 95) if sorted_runs else None,
        "p99": _percentile(sorted_runs, 99) if sorted_runs else None,
    }

    qual_improved = [
        r for r in successes
        if r["quality"] is not None
        and r["quality"]["ratio_to_gold"] > r["quality"]["ratio_to_input"]
    ]
    qual_echo = [
        r for r in successes
        if r["quality"] is not None and r["quality"]["equals_input_verbatim"]
    ]

    print()
    print("=" * 60)
    print("  Single-batch code-edit latency benchmark")
    print("=" * 60)
    print(f"Backend URL:       {url}")
    print(f"Samples (sent):    {len(results)} (warmup discarded: {args.warmup})")
    print(f"Completed:         {len(successes)}/{len(steady)} requests "
          f"({len(errors)} errors)")
    print(f"Improved-over-input: {len(qual_improved)}/{len(successes)}")
    print(f"Echoed-input verbatim: {len(qual_echo)}/{len(successes)}  (anti-bypass red flag)")
    print(f"Wall clock:        {wall_clock:.1f}s")
    print()
    print("Time to First Token (TTFT):")
    print(_fmt_stats(ttfts))
    print("Time per Output Token (TPOT):")
    print(_fmt_stats(tpots))
    print("Total Latency (end-to-end):")
    print(_fmt_stats(latencies))
    if output_tokens:
        med_tokens = statistics.median(output_tokens)
        med_latency = statistics.median(latencies) if latencies else float("nan")
        tok_per_s = med_tokens / med_latency if med_latency else float("nan")
        print(f"Median output tokens:  {med_tokens:.0f}")
        print(f"Median tok/s:          {tok_per_s:.1f}")

    print()
    print("Token-level diff vs prediction (drives headroom estimator):")
    print(f"  Aggregated match rate:  {aggregated_match_rate:.1%}")
    print(f"  Matched-run lengths (tokens): "
          f"p50={matched_run_pct['p50']} p75={matched_run_pct['p75']} "
          f"p90={matched_run_pct['p90']} p95={matched_run_pct['p95']}")

    p50_latency_ms = _percentile(sorted(latencies), 50) * 1000 if latencies else float("nan")
    median_tok_per_sec = (
        statistics.median(output_tokens) / statistics.median(latencies)
        if output_tokens and latencies and statistics.median(latencies)
        else float("nan")
    )
    print()
    print(f"Primary metric: median_tok_per_sec = {median_tok_per_sec:.2f}")
    print(f"  (also: p50_total_latency_ms = {p50_latency_ms:.1f})")
    print(f"Completed: {len(successes)}/{len(steady)} requests")

    chunk_counts = [r["num_chunks"] for r in successes]
    median_chunks = statistics.median(chunk_counts) if chunk_counts else None
    median_tok_per_chunk = (
        statistics.median(output_tokens) / median_chunks if median_chunks else None
    )
    result_dict: dict[str, Any] = {
        "config": {
            "url": url,
            "languages": languages,
            "num_samples": args.num_samples,
            "warmup": args.warmup,
            "max_tokens": args.max_tokens,
            "temperature": args.temperature,
            "seed": args.seed,
            "tokenizer_path": args.tokenizer_path,
        },
        "num_samples_sent": len(results),
        "num_steady": len(steady),
        "num_completed": len(successes),
        "num_failed": len(errors),
        "num_improved_over_input": len(qual_improved),
        "num_echo_input_verbatim": len(qual_echo),
        "actual_duration_sec": wall_clock,
        "ttft": _pct_block(sorted(ttfts)),
        "tpot": _pct_block(sorted(tpots)),
        "total_latency": _pct_block(sorted(latencies)),
        "median_output_tokens": statistics.median(output_tokens) if output_tokens else None,
        "median_chunks": median_chunks,
        "median_tokens_per_chunk": median_tok_per_chunk,
        "p50_total_latency_ms": p50_latency_ms,
        "median_tok_per_sec": median_tok_per_sec,
        # Aggregate token-level diff stats (input prediction vs model output).
        "aggregated_match_rate": aggregated_match_rate,
        "matched_run_pct_tokens": matched_run_pct,
        "per_sample": [
            {
                "sample_id": r["sample_id"],
                "language": r["language"],
                "bug_type": r["bug_type"],
                "tokens": r["output_tokens"],
                "chunks": r["num_chunks"],
                "ttft_ms": (r["ttft"] or 0) * 1000,
                "tpot_ms": (r["tpot"] or 0) * 1000 if r["tpot"] else None,
                "latency_ms": r["total_latency"] * 1000,
                "finish_reason": r["finish_reason"],
                "alignment": r["alignment"],
                "quality": r["quality"],
            }
            for r in successes
        ],
    }

    if errors:
        print("\nErrors:")
        for i, r in enumerate(errors[:5]):
            print(f"  [{i}] {r['error'][:120]}")
        if len(errors) > 5:
            print(f"  ... and {len(errors) - 5} more")

    if args.output_json:
        Path(args.output_json).write_text(json.dumps(result_dict, indent=2))
        print(f"\nResults written to {args.output_json}")

    return result_dict


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Single-batch code-edit latency benchmark using "
            "m-a-p/CodeEditorBench (code-debug rows). Drives an OpenAI-"
            "compatible /v1/completions server. Each request body carries "
            "an OpenAI-style `prediction.content` field; servers that "
            "consume it (predicted-outputs) score; servers that ignore it "
            "still get a valid request and run as normal completions."
        ),
    )
    parser.add_argument("--url", default="http://localhost:8000", help="Server base URL")
    parser.add_argument("--endpoint", default="/v1/completions", help="API endpoint path")
    parser.add_argument(
        "--model",
        default="",
        help="Model name to send in the request body (required by vLLM's "
             "OpenAI-compat endpoint). Leave empty for custom single-model "
             "servers that ignore the field.",
    )
    parser.add_argument(
        "--tokenizer-path",
        default="Qwen/Qwen3-32B",
        help="Tokenizer used for client-side chat templating + token counting.",
    )
    parser.add_argument(
        "--languages",
        default="python3",
        help="Comma-separated list of CodeEditorBench language tags to keep "
             "(python3, cpp, java). Default: python3 only — token alignment "
             "is the cleanest there.",
    )
    parser.add_argument(
        "--max-input-chars",
        type=int,
        default=4000,
        help="Skip rows whose buggy program is longer than this (default: 4000).",
    )
    parser.add_argument("--num-samples", type=int, default=50,
                        help="Total samples to send (default: 50).")
    parser.add_argument("--warmup", type=int, default=3,
                        help="Number of leading samples to discard from stats (default: 3).")
    parser.add_argument("--max-tokens", type=int, default=512,
                        help="Max tokens per response (default: 512).")
    parser.add_argument("--temperature", type=float, default=0,
                        help="Sampling temperature (default: 0 — greedy).")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--print-stream", action="store_true",
                        help="Print prompt/prediction and stream output deltas live to stderr.")
    parser.add_argument("--output-json", type=str, default=None,
                        help="Optional path to write structured results.")
    # Back-compat no-ops so the orchestrator's sanity invocation accepts these.
    parser.add_argument("--rate", type=float, default=None,
                        help="Ignored — single-batch only.")
    parser.add_argument("--num-requests", type=int, default=None,
                        help="Alias for --num-samples.")
    parser.add_argument("--duration", type=float, default=None,
                        help="Ignored — runs to --num-samples.")
    parser.add_argument("--prompt-len", type=int, default=None,
                        help="Ignored — prompts come from the dataset.")
    parser.add_argument("--audio-dir", type=str, default=None,
                        help="Ignored — text-only benchmark.")

    args = parser.parse_args()
    if args.num_requests is not None:
        args.num_samples = args.num_requests
    if args.warmup >= args.num_samples:
        args.warmup = max(0, args.num_samples - 1) if args.num_samples > 1 else 0
    asyncio.run(run_benchmark(args))


if __name__ == "__main__":
    main()
