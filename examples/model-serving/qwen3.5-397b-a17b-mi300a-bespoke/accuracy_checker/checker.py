#!/usr/bin/env python3
"""Accuracy checker for the from-scratch Qwen3.5 MI300A bundle.
Boots the candidate's ``python3 server.py`` (or, with ``--base-url``, reuses a
running one) and runs four deterministic gates over HTTP, all with thinking
disabled (``chat_template_kwargs.enable_thinking = false``):
1. Held-out multi-turn sessions replayed greedily: non-empty replies, sane
   finish reasons, ``usage.prompt_tokens`` strictly growing with the history
   (6 probes).
2. History-dependence probes: a fact planted in turn 1 must be recalled in
   turn 3 (4 probes).
3. Arithmetic sanity prompts (3 probes).
4. Greedy-token pins: for each entry of ``reference/pins.json`` the first
   ``EXACT_PREFIX_TOKENS`` greedy tokens must (nearly always) match a
   reference forward. See ``accuracy_checker/README.md`` for the schema, the
   budget, and how the pins are produced.
Gates 1-3 are 13 probes in total. Exit code 0 iff every gate passes. The pins
gate is mandatory: a missing ``reference/pins.json`` fails with instructions
unless ``--skip-pins`` is passed (local development only; the benchmark
harness never passes it).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import random
import re
import sys
from pathlib import Path

BUNDLE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BUNDLE_DIR / "benchmark"))
# lint-waiver: LW-008009 [E402]; This standalone bundle adds a sibling module directory to sys.path before importing its modules.
import launcher  # noqa: E402

# lint-waiver: LW-008010 [E402]; This standalone bundle adds a sibling module directory to sys.path before importing its modules.
from engine_scan import scan as scan_for_engine_code  # noqa: E402

DEFAULT_PINS_PATH = BUNDLE_DIR / "reference" / "pins.json"
PINS_SCHEMA_VERSION = 1
# Pin budget. Each pin is scored on its first EXACT_PREFIX_TOKENS greedy tokens.
# A pin "matches" when all of them are identical. The gate passes when at least
# MIN_EXACT_FRACTION of the pins match and every non-matching pin still agrees
# on at least MIN_DIVERGENCE_POSITION leading tokens (first mismatch index >=
# that value). The slack absorbs legitimate bf16/kernel-order differences that
# flip a near-tie argmax; a broken model diverges within the first tokens.
EXACT_PREFIX_TOKENS = 32
MIN_EXACT_FRACTION = 0.9
MIN_DIVERGENCE_POSITION = 8
MIN_PINS = 8
# Retokenizing the returned text may merge or split the final token, so an
# output this many tokens short of the prefix is not a divergence by itself.
RETOKENIZE_SLACK = 1
HOLDOUT_SEED = 20260226  # distinct from benchmark/run.py's seed.
N_HOLDOUT_SESSIONS = 6
HOLDOUT_TURNS = 4
HOLDOUT_MAX_TOKENS = 48
PROMPT_GROWTH_MIN = 1
PROMPT_GROWTH_MAX = 4000  # generous ceiling; catches non-growth or blowups, not exactness.
HOLDOUT_WORD_BANK = (
    "latency queue token cache memory schedule batch network response prompt "
    "session context history vector compute kernel thread process config "
    "deploy weight tensor layer route gateway cluster device driver runtime "
    "library metric report result summary status detail record value field"
).split()
BADGE_CODES = ("KX-7Q2R", "PL-3M9T", "QZ-8J1V", "RT-5K2X")
ARITHMETIC_PROMPTS: tuple[tuple[str, str], ...] = (
    ("What is 3 + 3? Answer with a single number and nothing else.", "6"),
    ("What is 12 - 5? Answer with a single number and nothing else.", "7"),
    ("What is 9 * 2? Answer with a single number and nothing else.", "18"),
)


@dataclasses.dataclass(slots=True)
class GateOutcome:
    name: str
    ok: bool
    detail: str


@dataclasses.dataclass(frozen=True, slots=True)
class Pin:
    pin_id: str
    messages: list[dict]
    expected_token_ids: list[int]


def _paragraph(rng: random.Random, min_words: int = 15, max_words: int = 60) -> str:
    n = rng.randint(min_words, max_words)
    text = " ".join(rng.choice(HOLDOUT_WORD_BANK) for _ in range(n))
    return text[:1].upper() + text[1:] + "."


async def _chat(
    client, base_url: str, messages: list[dict], max_tokens: int, *, ignore_eos: bool = False
) -> dict:
    """Send a non-streaming greedy chat completion (thinking off); return the JSON body."""
    import aiohttp

    url = base_url.rstrip("/") + "/v1/chat/completions"
    # Qwen3.5 thinks by default; with these small budgets the whole response
    # would be reasoning text and never reach the answer the gates look for.
    body = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if ignore_eos:
        body["ignore_eos"] = True
    async with client.post(url, json=body, timeout=aiohttp.ClientTimeout(total=120)) as resp:
        text = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {text[:500]}")
        return json.loads(text)


def _content(response: dict) -> str:
    return ((response.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


async def gate_holdout_sessions(client, base_url: str) -> list[GateOutcome]:
    outcomes = []
    for session_id in range(N_HOLDOUT_SESSIONS):
        rng = random.Random(f"{HOLDOUT_SEED}-{session_id}")
        history: list[dict] = []
        prompt_token_counts: list[int] = []
        session_ok = True
        session_detail = "ok"
        for turn_index in range(1, HOLDOUT_TURNS + 1):
            history.append({"role": "user", "content": _paragraph(rng)})
            try:
                response = await _chat(client, base_url, history, HOLDOUT_MAX_TOKENS)
            except Exception as exc:
                session_ok, session_detail = False, f"turn {turn_index}: request failed: {exc}"
                break
            choice = (response.get("choices") or [{}])[0]
            text = _content(response).strip()
            finish_reason = choice.get("finish_reason")
            prompt_tokens = (response.get("usage") or {}).get("prompt_tokens")
            if not text:
                session_ok, session_detail = False, f"turn {turn_index}: empty response"
                break
            if finish_reason not in ("stop", "length"):
                session_ok, session_detail = (
                    False,
                    f"turn {turn_index}: unexpected finish_reason={finish_reason!r}",
                )
                break
            if not isinstance(prompt_tokens, int):
                session_ok, session_detail = (
                    False,
                    f"turn {turn_index}: missing/non-integer usage.prompt_tokens",
                )
                break
            prompt_token_counts.append(prompt_tokens)
            history.append({"role": "assistant", "content": text})
        if session_ok and len(prompt_token_counts) >= 2:
            for prev, curr in zip(prompt_token_counts, prompt_token_counts[1:], strict=False):
                growth = curr - prev
                if not (PROMPT_GROWTH_MIN <= growth <= PROMPT_GROWTH_MAX):
                    session_ok, session_detail = (
                        False,
                        f"prompt_tokens growth {growth} out of tolerance "
                        f"[{PROMPT_GROWTH_MIN}, {PROMPT_GROWTH_MAX}]: {prompt_token_counts}",
                    )
                    break
        outcomes.append(GateOutcome(f"holdout_session[{session_id}]", session_ok, session_detail))
    return outcomes


async def gate_history_probes(client, base_url: str) -> list[GateOutcome]:
    outcomes = []
    for probe_id, code in enumerate(BADGE_CODES):
        name = f"history_probe[{probe_id}]"
        messages = [
            {
                "role": "user",
                "content": f"For this conversation, my badge code is {code}. Please acknowledge.",
            },
        ]
        follow_ups = (
            "What is today's date, roughly, in words? Just guess.",
            "What was my badge code? Answer with just the code.",
        )
        reply = ""
        failure = None
        for turn, follow_up in enumerate((None, *follow_ups), start=1):
            if follow_up is not None:
                messages.append({"role": "user", "content": follow_up})
            try:
                reply = _content(await _chat(client, base_url, messages, 32))
            except Exception as exc:
                failure = f"turn {turn} failed: {exc}"
                break
            messages.append({"role": "assistant", "content": reply})
        if failure is not None:
            outcomes.append(GateOutcome(name, False, failure))
            continue
        ok = code.upper() in reply.upper()
        outcomes.append(
            GateOutcome(
                name, ok, "ok" if ok else f"expected {code!r} in recall, got {reply[:200]!r}"
            )
        )
    return outcomes


async def gate_arithmetic(client, base_url: str) -> list[GateOutcome]:
    outcomes = []
    for prompt_id, (prompt, answer) in enumerate(ARITHMETIC_PROMPTS):
        name = f"arithmetic[{prompt_id}]"
        try:
            response = await _chat(client, base_url, [{"role": "user", "content": prompt}], 16)
        except Exception as exc:
            outcomes.append(GateOutcome(name, False, f"request failed: {exc}"))
            continue
        text = _content(response)
        ok = re.search(rf"(?<!\d){re.escape(answer)}(?!\d)", text) is not None
        outcomes.append(
            GateOutcome(
                name, ok, "ok" if ok else f"expected {answer!r} in response, got {text[:200]!r}"
            )
        )
    return outcomes


def load_pins(path: Path) -> list[Pin]:
    """Load and validate a pins file; raise ``RuntimeError`` with an actionable message."""
    if not path.is_file():
        raise RuntimeError(
            f"pins file not found: {path}. The greedy-token pin gate needs "
            "reference/pins.json in the bundle. Generate it once on the cluster with "
            "`python3 accuracy_checker/make_pins.py --model-path $MODEL_PATH` "
            "(see accuracy_checker/README.md). Pass --skip-pins only for local "
            "development; the benchmark harness never does."
        )
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != PINS_SCHEMA_VERSION:
        raise RuntimeError(f"{path}: expected an object with version == {PINS_SCHEMA_VERSION}")
    raw_pins = data.get("pins")
    if not isinstance(raw_pins, list) or len(raw_pins) < MIN_PINS:
        raise RuntimeError(f"{path}: needs a `pins` list with at least {MIN_PINS} entries")
    pins = []
    for index, raw in enumerate(raw_pins):
        where = f"{path}: pins[{index}]"
        if not isinstance(raw, dict):
            raise RuntimeError(f"{where} must be an object")
        messages = raw.get("messages")
        ids = raw.get("expected_token_ids")
        if not (
            isinstance(messages, list) and messages and all(isinstance(m, dict) for m in messages)
        ):
            raise RuntimeError(f"{where}.messages must be a non-empty list of chat messages")
        if not (isinstance(ids, list) and all(isinstance(t, int) for t in ids)):
            raise RuntimeError(f"{where}.expected_token_ids must be a list of ints")
        if len(ids) < EXACT_PREFIX_TOKENS:
            raise RuntimeError(
                f"{where}.expected_token_ids has {len(ids)} tokens; "
                f"at least {EXACT_PREFIX_TOKENS} required"
            )
        pins.append(Pin(str(raw.get("id", f"pin{index}")), messages, ids[:EXACT_PREFIX_TOKENS]))
    return pins


def first_divergence(expected: list[int], got: list[int]) -> int:
    """Index of the first mismatch, or ``len(expected)`` when the prefix fully matches.
    An output that is shorter than ``expected`` by more than ``RETOKENIZE_SLACK``
    tokens (all present tokens equal) diverges at its own length.
    """
    for index, (want, have) in enumerate(zip(expected, got, strict=False)):
        if want != have:
            return index
    if len(got) < len(expected) - RETOKENIZE_SLACK:
        return len(got)
    return len(expected)


def score_pins(divergences: dict[str, int], n_tokens: int = EXACT_PREFIX_TOKENS) -> GateOutcome:
    """Apply the budget to per-pin first-divergence indices."""
    exact = [p for p, d in divergences.items() if d >= n_tokens]
    early = {p: d for p, d in divergences.items() if d < MIN_DIVERGENCE_POSITION}
    fraction = len(exact) / len(divergences) if divergences else 0.0
    ok = fraction >= MIN_EXACT_FRACTION and not early
    tail = (
        f"early divergence (before token {MIN_DIVERGENCE_POSITION}): {early}"
        if early
        else f"no divergence before token {MIN_DIVERGENCE_POSITION}"
    )
    detail = (
        f"{len(exact)}/{len(divergences)} pins match {n_tokens} tokens "
        f"(need >= {MIN_EXACT_FRACTION:.0%}); {tail}"
    )
    return GateOutcome("greedy_pins", ok, detail)


async def gate_pins(client, base_url: str, pins: list[Pin], tokenizer) -> list[GateOutcome]:
    """Request each pin greedily; compare the retokenized output with the reference ids."""
    divergences: dict[str, int] = {}
    for pin in pins:
        try:
            response = await _chat(
                client, base_url, pin.messages, EXACT_PREFIX_TOKENS, ignore_eos=True
            )
        except Exception as exc:
            return [GateOutcome("greedy_pins", False, f"{pin.pin_id}: request failed: {exc}")]
        got = tokenizer.encode(_content(response), add_special_tokens=False)
        divergences[pin.pin_id] = first_divergence(pin.expected_token_ids, got)
    return [score_pins(divergences)]


async def run_checks(base_url: str, pins: list[Pin] | None, tokenizer) -> list[GateOutcome]:
    import aiohttp

    async with aiohttp.ClientSession() as client:
        outcomes = await gate_holdout_sessions(client, base_url)
        outcomes += await gate_history_probes(client, base_url)
        outcomes += await gate_arithmetic(client, base_url)
        if pins is not None:
            outcomes += await gate_pins(client, base_url, pins, tokenizer)
    return outcomes


def load_tokenizer(model_path: str):
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)


async def main_async(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    log_path = workspace / ".vibesys-accuracy-server.log"
    engine_findings = scan_for_engine_code(workspace)
    if engine_findings:
        print("ACCURACY CHECK FAILED: disallowed engine code (see OBJECTIVE.md):", file=sys.stderr)
        for finding in engine_findings:
            print(f"  - {finding}", file=sys.stderr)
        return 1
    try:
        model_path = args.model_path or launcher.resolve_model_path()
        pins = None if args.skip_pins else load_pins(Path(args.pins))
        tokenizer = None if pins is None else load_tokenizer(model_path)
        async with launcher.server_endpoint(
            base_url=args.base_url,
            workspace=workspace,
            model_path=model_path,
            host=args.host,
            port=args.port,
            log_path=log_path,
            startup_timeout_seconds=args.startup_timeout_seconds,
        ) as base_url:
            outcomes = await run_checks(base_url, pins, tokenizer)
    except Exception as exc:
        print(f"accuracy check could not run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    failures = [o for o in outcomes if not o.ok]
    for outcome in outcomes:
        status = "OK  " if outcome.ok else "FAIL"
        print(f"  {status} {outcome.name}: {outcome.detail}")
    print(f"\n{len(outcomes) - len(failures)}/{len(outcomes)} checks passed")
    if args.skip_pins:
        print("WARNING: --skip-pins was passed; the greedy-pin gate did not run.", file=sys.stderr)
    if args.output_json:
        Path(args.output_json).write_text(
            json.dumps(
                {"passed": not failures, "checks": [dataclasses.asdict(o) for o in outcomes]},
                indent=2,
            )
        )
    if failures:
        print("\nACCURACY CHECK FAILED:", file=sys.stderr)
        for outcome in failures:
            print(f"  - {outcome.name}: {outcome.detail}", file=sys.stderr)
        return 1
    print("\nACCURACY CHECK PASSED")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace", default=".", help="Workspace root containing server.py (default: cwd)."
    )
    parser.add_argument(
        "--model-path", default=None, help="Model directory. Defaults to $MODEL_PATH."
    )
    parser.add_argument("--host", default=launcher.DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=launcher.DEFAULT_PORT)
    parser.add_argument(
        "--startup-timeout-seconds", type=float, default=launcher.STARTUP_TIMEOUT_SECONDS
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Run against an already-running server at this URL instead of booting one.",
    )
    parser.add_argument("--pins", default=str(DEFAULT_PINS_PATH), help="Pins file.")
    parser.add_argument(
        "--skip-pins",
        action="store_true",
        help="Skip the greedy-token pin gate. Local development only; the harness never passes it.",
    )
    parser.add_argument("--output-json", default=None, help="Optional path for a JSON report.")
    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
