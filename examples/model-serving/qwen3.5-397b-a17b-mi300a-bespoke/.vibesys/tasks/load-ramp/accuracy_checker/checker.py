#!/usr/bin/env python3
"""Accuracy checker for the from-scratch Qwen3.5 MI300A load-ramp task.

Runs against the candidate's server at ``--base-url`` (default
``$VIBESYS_SERVICE_URL``, which vibesys-cli sets for a task of two images).
Exit code 0 iff every gate passes. In order:

1. Disallowed engine code (``engine_scan.py``) in the candidate's checkout at
   ``--workspace``: any finding fails before a request is sent.
2. 13 behavioral probes, with thinking disabled
   (``chat_template_kwargs.enable_thinking = false``): 6 held-out multi-turn
   sessions replayed greedily (non-empty replies, sane finish reasons,
   ``usage.prompt_tokens`` strictly growing with the history), 4 history
   recalls (a fact planted in turn 1 recalled in turn 3) and 3 arithmetic
   prompts.
3. The numerics gate the optimization campaign accepted every change by,
   against the campaign's reference (``reference/``, see its README):
   - teacher-forced top-1: each of the 34 reference prompts plus its 32-token
     reference continuation goes through ``POST /v1/score``; at least
     ``TEACHER_TOP1_MIN_MATCHES`` of the ``TEACHER_TOP1_TOTAL`` positions must
     have the reference's top-1 token as the candidate's top-1 token, and
     every prompt must be scored at every position;
   - greedy completions: each of the 5 reference prompts, greedy with natural
     EOS and at most 64 tokens, must return exactly the reference's text.

``--output-json`` writes every gate's outcome, and the numerics gate's counts,
for people.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import random
import re
import sys
from pathlib import Path

TASK_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
# lint-waiver: LW-018704 [E402]; This standalone task script adds its own directory to sys.path before importing its sibling module.
from engine_scan import scan as scan_for_engine_code  # noqa: E402

SERVICE_URL_ENV = "VIBESYS_SERVICE_URL"
ARTIFACTS_DIR_ENV = "VIBESYS_ARTIFACTS_DIR"
REFERENCE_DIR = TASK_DIR / "reference"

# The campaign's numerics gate: 34 prompts x 32 teacher-forced positions, of
# which at least 1,074 (98.7%) must agree on the top-1 token, and 5 of 5 greedy
# completions identical to the reference's.
TEACHER_TOP1_TOTAL = 1088
TEACHER_TOP1_MIN_MATCHES = 1074
GREEDY_PROMPTS = 5
GREEDY_MAX_TOKENS = 64
# One request's bound, for the client: the longest scored prompt is about 8,000
# tokens through a candidate that may prefill slowly. It only guards a hang.
REQUEST_TIMEOUT_S = 900.0

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
class ScoredPrompt:
    """One teacher-forced reference prompt: its token ids, continuation and top-1 tokens."""

    prompt_id: str
    prompt_ids: tuple[int, ...]
    continuation_ids: tuple[int, ...]
    reference_top1: tuple[int, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class GreedyPrompt:
    """One greedy reference prompt and the reference's completion text."""

    prompt_id: str
    messages: tuple[dict, ...]
    text: str


@dataclasses.dataclass(frozen=True, slots=True)
class Reference:
    """The campaign's numerics reference, as ``load_reference`` validated it."""

    scored: tuple[ScoredPrompt, ...]
    greedy: tuple[GreedyPrompt, ...]


def _paragraph(rng: random.Random, min_words: int = 15, max_words: int = 60) -> str:
    n = rng.randint(min_words, max_words)
    text = " ".join(rng.choice(HOLDOUT_WORD_BANK) for _ in range(n))
    return text[:1].upper() + text[1:] + "."


async def _post_json(client, url: str, body: dict) -> dict:
    """POST ``body`` and return the JSON reply; any status but 200 raises naming it."""
    async with client.post(url, json=body) as resp:
        text = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {text[:500]}")
        return json.loads(text)


async def _chat(client, base_url: str, messages: list[dict], max_tokens: int) -> dict:
    """Send a non-streaming greedy chat completion (thinking off); return the JSON body."""
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
    return await _post_json(client, url, body)


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


# --- The campaign's numerics reference -----------------------------------------------------


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: cannot be read as JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def _int_list(value: object, where: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not all(
        isinstance(t, int) and not isinstance(t, bool) for t in value
    ):
        raise ValueError(f"{where}: expected a list of token ids")
    return tuple(value)


def _rows(data: dict, key: str, where: Path) -> list[dict]:
    rows = data.get(key)
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise ValueError(f"{where}: `{key}` must be a list of objects")
    return rows


def _scored_prompts(directory: Path) -> tuple[ScoredPrompt, ...]:
    continuations_path = directory / "campaign_continuations.json"
    scores_path = directory / "campaign_scores.json"
    continuations = _rows(_read_json(continuations_path), "pins", continuations_path)
    scores = {row.get("id"): row for row in _rows(_read_json(scores_path), "pins", scores_path)}
    if {row.get("id") for row in continuations} != scores.keys():
        raise ValueError(f"{scores_path}: its ids are not those of {continuations_path}")
    scored = []
    for row in continuations:
        prompt_id = str(row["id"])
        where = f"{continuations_path}: {prompt_id}"
        prompt_ids = _int_list(row.get("prompt_ids"), f"{where}.prompt_ids")
        continuation = _int_list(row.get("continuation_ids"), f"{where}.continuation_ids")
        top1 = _int_list(scores[prompt_id].get("top1"), f"{scores_path}: {prompt_id}.top1")
        if (
            not prompt_ids
            or len(top1) != len(continuation)
            or scores[prompt_id].get("ok") is not True
        ):
            raise ValueError(f"{scores_path}: {prompt_id} is not a complete reference row")
        scored.append(ScoredPrompt(prompt_id, prompt_ids, continuation, top1))
    if sum(len(p.reference_top1) for p in scored) != TEACHER_TOP1_TOTAL:
        raise ValueError(f"{scores_path}: expected {TEACHER_TOP1_TOTAL} scored positions")
    return tuple(scored)


def _greedy_prompts(directory: Path, messages: dict[str, list]) -> tuple[GreedyPrompt, ...]:
    path = directory / "campaign_greedy.json"
    rows = _rows(_read_json(path), "prompts", path)
    if len(rows) != GREEDY_PROMPTS:
        raise ValueError(f"{path}: expected {GREEDY_PROMPTS} prompts, found {len(rows)}")
    greedy = []
    for index, row in enumerate(rows):
        text = row.get("text")
        if not isinstance(text, str):
            raise ValueError(f"{path}: prompts[{index}].text must be a string")
        # The greedy prompts are the first five reference prompts (pin00..pin04).
        greedy.append(GreedyPrompt(str(row.get("id")), tuple(messages[f"pin{index:02d}"]), text))
    return tuple(greedy)


def load_reference(directory: Path = REFERENCE_DIR) -> Reference:
    """Read and validate the campaign's reference; raise ``ValueError`` naming the file."""
    continuations_path = directory / "campaign_continuations.json"
    messages = {
        str(row.get("id")): row.get("messages")
        for row in _rows(_read_json(continuations_path), "pins", continuations_path)
    }
    if not all(isinstance(m, list) and m for m in messages.values()):
        raise ValueError(f"{continuations_path}: every row needs non-empty `messages`")
    return Reference(_scored_prompts(directory), _greedy_prompts(directory, messages))


def _top1_tokens(reply: dict, positions: int) -> tuple[int, ...]:
    """The candidate's top-1 token at each scored position of a ``/v1/score`` reply."""
    scores = reply.get("scores")
    if not isinstance(scores, list) or len(scores) != positions:
        raise ValueError(f"`scores` must list {positions} positions")
    tokens = []
    for index, row in enumerate(scores):
        top = (row.get("top_logprobs") or [None])[0] if isinstance(row, dict) else None
        token = top.get("token_id") if isinstance(top, dict) else None
        if not isinstance(token, int) or isinstance(token, bool):
            raise ValueError(f"scores[{index}].top_logprobs[0].token_id must be a token id")
        tokens.append(token)
    return tuple(tokens)


async def gate_teacher_top1(
    client, base_url: str, prompts: tuple[ScoredPrompt, ...]
) -> GateOutcome:
    """Teacher-force every reference continuation; count positions whose top-1 agrees."""
    url = base_url.rstrip("/") + "/v1/score"
    matches = 0
    failures: list[str] = []
    for prompt in prompts:
        body = {
            "prompt": [*prompt.prompt_ids, *prompt.continuation_ids],
            "continuation_start": len(prompt.prompt_ids),
            "top_k": 1,
        }
        try:
            top1 = _top1_tokens(await _post_json(client, url, body), len(prompt.reference_top1))
        except Exception as exc:
            failures.append(f"{prompt.prompt_id}: {type(exc).__name__}: {exc}")
            continue
        matches += sum(a == b for a, b in zip(prompt.reference_top1, top1, strict=True))
    ok = not failures and matches >= TEACHER_TOP1_MIN_MATCHES
    detail = (
        f"{matches}/{TEACHER_TOP1_TOTAL} top-1 tokens agree (need >= {TEACHER_TOP1_MIN_MATCHES})"
    )
    if failures:
        detail += f"; not scored: {'; '.join(failures)[:800]}"
    return GateOutcome("teacher_top1", ok, detail)


async def gate_greedy_reference(
    client, base_url: str, prompts: tuple[GreedyPrompt, ...]
) -> GateOutcome:
    """Each greedy completion (natural EOS, at most 64 tokens) must equal the reference's text.

    The campaign compared the retokenized token ids of the two texts; the text a
    server returns is the decode of its token ids, so equal texts are equal ids.
    """
    url = base_url.rstrip("/") + "/v1/chat/completions"
    mismatched: list[str] = []
    for prompt in prompts:
        body = {
            "messages": list(prompt.messages),
            "max_tokens": GREEDY_MAX_TOKENS,
            "temperature": 0,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        try:
            text = _content(await _post_json(client, url, body))
        except Exception as exc:
            mismatched.append(f"{prompt.prompt_id}: {type(exc).__name__}: {exc}")
            continue
        if text != prompt.text:
            mismatched.append(f"{prompt.prompt_id}: got {text[:120]!r}")
    matches = len(prompts) - len(mismatched)
    detail = f"{matches}/{len(prompts)} greedy completions identical to the reference"
    if mismatched:
        detail += f"; differ: {'; '.join(mismatched)[:800]}"
    return GateOutcome("greedy_reference", not mismatched, detail)


# --- Driver ---------------------------------------------------------------------------------


def open_http_client():
    """The real HTTP client, every request bounded by ``REQUEST_TIMEOUT_S``."""
    # lint-waiver: LW-018705 [PLC0415]; aiohttp is imported where the real client is opened.
    # > A module-level import would make the reference loader and the gates'
    # > tests need aiohttp installed; nothing else in the module uses it.
    import aiohttp  # noqa: PLC0415

    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S))


async def run_checks(client, base_url: str, reference: Reference) -> list[GateOutcome]:
    """Every gate after the engine scan, in order, against one server."""
    outcomes = await gate_holdout_sessions(client, base_url)
    outcomes += await gate_history_probes(client, base_url)
    outcomes += await gate_arithmetic(client, base_url)
    outcomes.append(await gate_teacher_top1(client, base_url, reference.scored))
    outcomes.append(await gate_greedy_reference(client, base_url, reference.greedy))
    return outcomes


def report(outcomes: list[GateOutcome], output_json: str | None) -> int:
    """Print every outcome, write the JSON report when asked, and return the exit code."""
    failures = [o for o in outcomes if not o.ok]
    for outcome in outcomes:
        status = "OK  " if outcome.ok else "FAIL"
        print(f"  {status} {outcome.name}: {outcome.detail}")
    print(f"\n{len(outcomes) - len(failures)}/{len(outcomes)} checks passed")
    if output_json:
        Path(output_json).write_text(
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


async def main_async(args: argparse.Namespace, open_client=open_http_client) -> int:
    findings = scan_for_engine_code(Path(args.workspace).resolve())
    if findings:
        outcome = GateOutcome("engine_scan", False, "; ".join(findings)[:2000])
        return report([outcome], args.output_json)
    try:
        reference = load_reference()
    except ValueError as exc:
        print(f"accuracy check could not run: {exc}", file=sys.stderr)
        return 1
    async with open_client() as client:
        outcomes = await run_checks(client, args.base_url, reference)
    return report([GateOutcome("engine_scan", True, "no findings"), *outcomes], args.output_json)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=".", help="The candidate's checkout (default: cwd).")
    parser.add_argument(
        "--base-url",
        default=os.environ.get(SERVICE_URL_ENV),
        help=f"The candidate's server (default: ${SERVICE_URL_ENV}).",
    )
    artifacts = os.environ.get(ARTIFACTS_DIR_ENV)
    parser.add_argument(
        "--output-json",
        default=str(Path(artifacts) / "accuracy-report.json") if artifacts else None,
        help=f"Path for a JSON report (default: ${ARTIFACTS_DIR_ENV}/accuracy-report.json).",
    )
    args = parser.parse_args()
    if not args.base_url:
        parser.error(f"--base-url is required when ${SERVICE_URL_ENV} is not set")
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
