"""
Accuracy checker for the Llama-3-8B serving system (service-style).

This checker drives a *running* OpenAI-compatible server over HTTP — it does
NOT import the candidate's model or load any weights locally. That makes it
work identically whether the server runs on the local host, in Docker, or on a
remote Modal GPU: the checker only needs the server URL.

Because there is no local GPU reference to diff against, correctness is
established with three reference-free gates that a real Llama-3 forward pass
passes and reward-hacking shortcuts (canned text, prompt echoers, schema
synthesizers) fail:

  1. Sentinel-echo rate  — each request embeds a random sentinel token the
     prompt instructs the model to reproduce. A server that ignores the prompt
     and returns canned/templated text cannot reproduce a fresh random token.
     (This is the framework's canonical anti-reward-hack gate.)

  2. Known-answer rate   — near-deterministic factual prompts at temperature 0
     whose answer is fixed (capital of France -> Paris, 1+1 -> 2, ...). A
     prompt echoer passes the sentinel gate but fails this one; a canned
     "Paris" server fails the sentinel gate. Only a model that actually runs
     inference passes both.

  3. Greedy determinism  — the same prompt sent twice at temperature 0 must
     yield identical output. Catches nondeterministic / sampling-when-it-should-
     not decoders.

Exit code 0 iff there are no transport errors AND all three gates clear their
thresholds; exit 1 otherwise.

Usage (server must already be running):

    python checker.py --url http://localhost:8000
    python checker.py --url https://<app>.modal.run --seed 0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import string
import sys

import httpx

# ---------------------------------------------------------------------------
# HTTP helpers — stream SSE from the OpenAI-compatible endpoints
# ---------------------------------------------------------------------------


async def _stream_text(
    client: httpx.AsyncClient,
    url: str,
    body: dict,
    request_timeout: float,
) -> tuple[str, str | None]:
    """POST a streaming request and concatenate the generated text.

    Returns ``(text, error)``. ``error`` is None on success. Handles both the
    completions shape (``choices[0].text``) and the chat shape
    (``choices[0].delta.content``).
    """
    parts: list[str] = []
    try:
        async with client.stream("POST", url, json=body, timeout=request_timeout) as resp:
            resp.raise_for_status()
            async for raw in resp.aiter_lines():
                if not raw.startswith("data: "):
                    continue
                payload = raw[len("data: ") :]
                if payload.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choice = (chunk.get("choices") or [{}])[0]
                # completions vs chat
                text = choice.get("text")
                if text is None:
                    text = (choice.get("delta") or {}).get("content")
                if text:
                    parts.append(text)
    except Exception as exc:  # noqa: BLE001 - report any transport failure
        return "".join(parts), f"{type(exc).__name__}: {exc}"
    return "".join(parts), None


async def complete(
    client: httpx.AsyncClient,
    base_url: str,
    endpoint: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    request_timeout: float,
) -> tuple[str, str | None]:
    """Raw /v1/completions call."""
    url = base_url.rstrip("/") + endpoint
    body = {
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    return await _stream_text(client, url, body, request_timeout)


async def chat(
    client: httpx.AsyncClient,
    base_url: str,
    endpoint: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    request_timeout: float,
) -> tuple[str, str | None]:
    """Chat /v1/chat/completions call."""
    url = base_url.rstrip("/") + endpoint
    body = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    return await _stream_text(client, url, body, request_timeout)


# ---------------------------------------------------------------------------
# Gate 1: sentinel echo
# ---------------------------------------------------------------------------


def _make_sentinel(rng: random.Random) -> str:
    """A short, tokenizer-friendly random uppercase word unlikely to be canned."""
    return "".join(rng.choice(string.ascii_uppercase) for _ in range(8))


async def gate_sentinel_echo(
    client: httpx.AsyncClient,
    args: argparse.Namespace,
    rng: random.Random,
) -> list[dict]:
    results: list[dict] = []
    for _ in range(args.num_sentinel):
        sentinel = _make_sentinel(rng)
        messages = [
            {
                "role": "user",
                "content": (
                    f"Repeat the following word exactly once, in uppercase, and "
                    f"output nothing else: {sentinel}"
                ),
            }
        ]
        text, err = await chat(
            client,
            args.url,
            args.chat_endpoint,
            messages,
            max_tokens=16,
            temperature=0.0,
            request_timeout=args.request_timeout,
        )
        ok = err is None and sentinel in text.upper()
        results.append(
            {
                "gate": "sentinel",
                "sentinel": sentinel,
                "output": text,
                "error": err,
                "ok": ok,
            }
        )
    return results


# ---------------------------------------------------------------------------
# Gate 2: known-answer factual prompts (chat, temperature 0)
# ---------------------------------------------------------------------------

# (user message, list of acceptable answer substrings [case-insensitive])
KNOWN_ANSWERS: list[tuple[str, list[str]]] = [
    ("What is the capital of France? Answer with a single word.", ["paris"]),
    ("What is 1 + 1? Answer with a single number.", ["2", "two"]),
    ("What is the opposite of 'hot'? Answer with a single word.", ["cold"]),
    ("What color is a clear daytime sky? Answer with a single word.", ["blue"]),
    ("What is 7 multiplied by 6? Answer with a single number.", ["42", "forty-two", "forty two"]),
    ("How many days are in a week? Answer with a single number.", ["7", "seven"]),
    ("What is the chemical symbol for water? Answer with a single token.", ["h2o", "h₂o"]),
    ("Complete the sequence with one number: 2, 4, 6, 8,", ["10", "ten"]),
]


async def gate_known_answers(
    client: httpx.AsyncClient,
    args: argparse.Namespace,
) -> list[dict]:
    results: list[dict] = []
    for question, accepted in KNOWN_ANSWERS:
        messages = [{"role": "user", "content": question}]
        text, err = await chat(
            client,
            args.url,
            args.chat_endpoint,
            messages,
            max_tokens=24,
            temperature=0.0,
            request_timeout=args.request_timeout,
        )
        low = text.lower()
        ok = err is None and any(a in low for a in accepted)
        results.append(
            {
                "gate": "known_answer",
                "question": question,
                "accepted": accepted,
                "output": text,
                "error": err,
                "ok": ok,
            }
        )
    return results


# ---------------------------------------------------------------------------
# Gate 3: greedy determinism (same prompt twice at temperature 0 == identical)
# ---------------------------------------------------------------------------

DETERMINISM_PROMPTS: list[str] = [
    "The capital of France is",
    "Once upon a time, in a land far away,",
    "def fibonacci(n):\n    # return the n-th Fibonacci number\n",
    "The following is a list of the planets in the Solar System:",
]


async def gate_determinism(
    client: httpx.AsyncClient,
    args: argparse.Namespace,
) -> list[dict]:
    results: list[dict] = []
    for prompt in DETERMINISM_PROMPTS:
        out_a, err_a = await complete(
            client, args.url, args.endpoint, prompt, args.det_max_tokens, 0.0, args.request_timeout
        )
        out_b, err_b = await complete(
            client, args.url, args.endpoint, prompt, args.det_max_tokens, 0.0, args.request_timeout
        )
        err = err_a or err_b
        ok = err is None and out_a == out_b and len(out_a) > 0
        results.append(
            {
                "gate": "determinism",
                "prompt": prompt,
                "output_a": out_a,
                "output_b": out_b,
                "error": err,
                "ok": ok,
            }
        )
    return results


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _rate(results: list[dict]) -> tuple[int, int, float]:
    ok = sum(1 for r in results if r["ok"])
    n = len(results)
    return ok, n, (ok / n if n else 0.0)


async def run(args: argparse.Namespace) -> int:
    rng = random.Random(args.seed)
    print(f"Accuracy check against {args.url} (seed={args.seed})")
    print(
        f"  gates: sentinel-echo (n={args.num_sentinel}, min {args.min_sentinel_rate:.0%}), "
        f"known-answer (n={len(KNOWN_ANSWERS)}, min {args.min_known_rate:.0%}), "
        f"determinism (n={len(DETERMINISM_PROMPTS)}, min {args.min_determinism_rate:.0%})"
    )

    async with httpx.AsyncClient() as client:
        sentinel = await gate_sentinel_echo(client, args, rng)
        known = await gate_known_answers(client, args)
        determinism = await gate_determinism(client, args)

    all_results = sentinel + known + determinism
    transport_errors = [r for r in all_results if r["error"] is not None]

    s_ok, s_n, s_rate = _rate(sentinel)
    k_ok, k_n, k_rate = _rate(known)
    d_ok, d_n, d_rate = _rate(determinism)

    # Print per-gate detail
    for label, rows in (
        ("SENTINEL", sentinel),
        ("KNOWN-ANSWER", known),
        ("DETERMINISM", determinism),
    ):
        print(f"\n{label}")
        for r in rows:
            status = "OK  " if r["ok"] else "FAIL"
            preview = (r.get("output") or r.get("output_a") or "").strip().replace("\n", " ")[:70]
            extra = r.get("sentinel") or r.get("question") or r.get("prompt") or ""
            extra = str(extra).replace("\n", " ")[:50]
            err = f" err={r['error']}" if r["error"] else ""
            print(f"  {status} [{extra!r}] -> {preview!r}{err}")

    print("\n" + "=" * 60)
    print("  Llama-3-8B service accuracy check")
    print("=" * 60)
    print(f"Sentinel-echo:   {s_ok}/{s_n} ({s_rate:.0%})   [min {args.min_sentinel_rate:.0%}]")
    print(f"Known-answer:    {k_ok}/{k_n} ({k_rate:.0%})   [min {args.min_known_rate:.0%}]")
    print(f"Determinism:     {d_ok}/{d_n} ({d_rate:.0%})   [min {args.min_determinism_rate:.0%}]")
    print(f"Transport errors: {len(transport_errors)}")

    passed = (
        not transport_errors
        and s_rate >= args.min_sentinel_rate
        and k_rate >= args.min_known_rate
        and d_rate >= args.min_determinism_rate
    )

    if args.output_json:
        from pathlib import Path

        summary = {
            "url": args.url,
            "seed": args.seed,
            "sentinel_rate": s_rate,
            "known_answer_rate": k_rate,
            "determinism_rate": d_rate,
            "num_transport_errors": len(transport_errors),
            "thresholds": {
                "min_sentinel_rate": args.min_sentinel_rate,
                "min_known_rate": args.min_known_rate,
                "min_determinism_rate": args.min_determinism_rate,
            },
            "passed": passed,
            "results": all_results,
        }
        Path(args.output_json).write_text(json.dumps(summary, indent=2))
        print(f"\nWrote detailed results to {args.output_json}")

    print("\n" + ("ACCURACY CHECK PASSED" if passed else "ACCURACY CHECK FAILED"))
    return 0 if passed else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Service-style accuracy checker for the Llama-3-8B server. Drives a "
            "running OpenAI-compatible server over HTTP (no local weights) and "
            "asserts sentinel-echo, known-answer, and greedy-determinism gates."
        )
    )
    parser.add_argument("--url", default="http://localhost:8000", help="Server base URL")
    parser.add_argument("--endpoint", default="/v1/completions", help="Completions endpoint path")
    parser.add_argument(
        "--chat-endpoint", default="/v1/chat/completions", help="Chat completions endpoint path"
    )
    parser.add_argument(
        "--num-sentinel", type=int, default=6, help="Number of sentinel-echo probes"
    )
    parser.add_argument(
        "--det-max-tokens", type=int, default=32, help="Tokens per determinism probe"
    )
    parser.add_argument(
        "--seed",
        type=lambda s: None if s.lower() in ("none", "random") else int(s),
        default=0,
        help="RNG seed for sentinels (int, or 'random').",
    )
    parser.add_argument("--min-sentinel-rate", type=float, default=0.90)
    parser.add_argument("--min-known-rate", type=float, default=0.75)
    parser.add_argument("--min-determinism-rate", type=float, default=0.90)
    parser.add_argument("--request-timeout", type=float, default=120.0)
    parser.add_argument("--output-json", type=str, default=None)
    args = parser.parse_args()

    rc = asyncio.run(run(args))
    sys.exit(rc)


if __name__ == "__main__":
    main()
