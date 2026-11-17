#!/usr/bin/env python3
"""Load-ramp multi-turn chat benchmark for the from-scratch Qwen3.5 MI300A task.

Drives a concurrency ramp of deterministic multi-turn chat sessions against the
candidate's OpenAI-compatible ``/v1/chat/completions`` endpoint, at
``--base-url`` (default ``$VIBESYS_SERVICE_URL``, which vibesys-cli sets to the
candidate's server for a task of two images), and reports the metrics through
two independent outputs:

- ``--vs-output``: the VibeSys evaluator result protocol v2 record stream (see
  ``vs_protocol.py``). This is what the framework scores: ``peak_goodput_tok_s``
  (max) and ``p95_ttft_turn2plus_ms_at_ref`` (min), see ``objectives.toml``.
- ``--output-json``: a human-readable flat JSON object plus the full per-level
  ramp table, for manual runs and campaign analysis. Defaults to
  ``<vs-output>.metrics.json`` when only ``--vs-output`` is given. At least one
  of the two is required. Per-turn records go beside it, in
  ``<output-json>.turns.jsonl``.

Workload (``--ramp``, see ``run_ramp``): an in-flight load controller holds
exactly ``C`` requests concurrent for each level of a concurrency ramp
(``DEFAULT_RAMP_LEVELS`` unless overridden), drawing turns from an unbounded
deterministic session stream (``make_session``). Per level: a warmup, then a
measurement window (extended if too few turns were sent), from which
throughput and latency percentiles are computed. The ramp stops on a
throughput plateau, a latency-guardrail breach, or an invalid level (any
failed/dropped/empty/short turn); an invalid first level or reference-
concurrency level (``C_REF``) is a hard error for the whole run. After the
last level the workers drain, and every level is recomputed from the drained
turns (``_finalize_ramp_levels``), so a turn in flight at a window's end is
counted. See OBJECTIVE.md for the stop rule and partial-run semantics.

The scored schedule is C16 then C96 (``DEFAULT_RAMP_LEVELS``): C16 is the
reference level (``C_REF``) whose turn-2+ p95 TTFT is the latency objective,
and C96 is the concurrency the optimization campaign this harness comes from
reported its goodput at. ``--ramp`` runs any other strictly increasing
schedule, for example the campaign's full ladder
``1,2,4,8,16,32,48,64,96,128,192,256``.

Session shape (fixed, deterministic, seeded, not a tunable of this script):
3-6 turns per session, user messages are pseudo-paragraphs of varying length,
greedy decoding with ``ignore_eos`` and a per-turn fixed output budget, 1-8s
think-time between a session's turns.

``--quick``: a fast, non-scoring preset for exploration (single C=48 level,
short warmup/window, no extension, a mid-window fail-fast abort on a clear
guardrail breach; see the CLI help and ``QUICK_RAMP_LEVELS``).
``--output-json``'s payload marks ``scoring: false`` for these runs; only the
default schedule without ``--quick`` is scored.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import heapq
import json
import os
import random
import sys
import time
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Literal, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))
# lint-waiver: LW-018701 [E402]; This standalone task script adds its own directory to sys.path before importing its sibling module.
import vs_protocol  # noqa: E402

# lint-waiver: LW-018702 [E402]; This standalone task script adds its own directory to sys.path before importing its sibling module.
from vs_protocol import MetricSpec  # noqa: E402

# Workload/objective-model version. Version 5 introduced the concurrency ramp.
# Version 6 fixes right-censored ramp results by reporting each level only
# after active requests have drained at the end of the run.
BENCHMARK_VERSION = 6

SEED = 20260225
N_SESSIONS = 48  # historical session count; the ramp's session stream is unbounded.
MIN_TURNS = 3
MAX_TURNS = 6
MIN_WORDS = 30  # ~40 tokens
MAX_WORDS = 300  # ~400 tokens
MIN_MAX_TOKENS = 80
MAX_MAX_TOKENS = 300
THINK_TIME_MIN_S = 1.0
THINK_TIME_MAX_S = 8.0
REQUEST_TIMEOUT_S = 300.0
# The candidate's server, as vibesys-cli names it to a task of two images.
SERVICE_URL_ENV = "VIBESYS_SERVICE_URL"
# Where vibesys-cli keeps the files of a job's command run.
ARTIFACTS_DIR_ENV = "VIBESYS_ARTIFACTS_DIR"


def artifact_path(name: str) -> str | None:
    """``name`` in the job's artifacts directory, or None outside a vibesys-cli job."""
    directory = os.environ.get(ARTIFACTS_DIR_ENV)
    return str(Path(directory) / name) if directory else None


# The concurrency ramp: levels always run in increasing order, and workers are
# only ever added at a level boundary, never torn down (see run_ramp).
# The scored schedule: the reference level, then the campaign's headline level.
DEFAULT_RAMP_LEVELS: tuple[int, ...] = (16, 96)
DEFAULT_WARMUP_S = 15.0
DEFAULT_WINDOW_S = 60.0
# A level's measurement window extends (in --window-s increments) when too
# few turns were sent, up to this multiple of --window-s total, so a very
# slow server does not extend the window forever.
DEFAULT_WINDOW_EXTENSION_MULTIPLIER = 4.0

C_REF = 16
GUARDRAIL_TPOT_P95_MS = 250.0
GUARDRAIL_TTFT_TURN2PLUS_P95_MS = 10_000.0
PLATEAU_GAIN_THRESHOLD = 0.05
PLATEAU_CONSECUTIVE_LEVELS = 2

# --quick: a fast, non-scoring preset for the optimization loop's inner
# iterations (see the --quick CLI help and OBJECTIVE.md). One level at C=48
# (the historical session count), a short warmup, and a window sized to
# target roughly 2 minutes wall time per invocation end to end: warmup (10s)
# + window (50s, within the 45-60s a fast-iteration run needs for a
# reasonably stable p95 at this concurrency) = 60s of measurement, leaving
# about a minute of margin for process startup, worker spin-up/teardown, and
# result-file I/O -- overhead the scored path already pays too, just
# amortized over many more levels. No extension: a quick run must return in
# roughly constant time, not stretch out on a slow server the way the scored
# path deliberately does.
QUICK_RAMP_LEVELS: tuple[int, ...] = (48,)
QUICK_WARMUP_S = 10.0
QUICK_WINDOW_S = 50.0
QUICK_WINDOW_EXTENSION_MULTIPLIER = 1.0  # no extension: quick mode never waits past one window.

# Fail-fast mid-window check (quick mode only; see RampConfig.early_abort_enabled
# and _early_abort_breach): a level whose p95 TPOT is already this many times
# its guardrail, with at least this many completed turns already sampled, is
# not going to recover by waiting out the rest of the window, so quick mode
# stops measuring it immediately instead of burning the rest of the window.
# The turn-count floor keeps a handful of slow turns at the very start of a
# level (server/connection ramp-up) from tripping a false abort.
EARLY_ABORT_GUARDRAIL_MULTIPLIER = 2.0
EARLY_ABORT_MIN_TURNS = 30
EARLY_ABORT_POLL_INTERVAL_S = 5.0

WORD_BANK = (
    "system model request latency queue token cache memory schedule batch "
    "network server client stream response prompt session context history "
    "vector matrix compute kernel thread process launch config deploy build "
    "quantize weight tensor layer attention decode prefill router expert "
    "gateway cluster node device driver runtime library package module test "
    "metric report result summary status detail record value field data set "
    "train eval sample input output error warning info debug trace signal "
    "engine backend frontend service pipeline stage worker task job event "
    "policy plan design pattern architecture interface protocol format "
    "version release patch update install package script tool utility "
    "helper wrapper adapter bridge proxy relay channel socket port address "
    "region zone cluster shard partition replica backup restore snapshot "
    "checkpoint state transition graph tree list array buffer pool cache "
    "index lookup search filter sort merge split join reduce map fold scan"
).split()


class Clock(Protocol):
    """Where the benchmark reads time and waits: the system's clock, or a test's virtual one."""

    def monotonic(self) -> float:
        """Seconds on a monotonic clock, for every interval the benchmark measures."""
        ...

    def wall(self) -> float:
        """Seconds since the epoch, only for the per-turn records' timestamps."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait ``seconds``; zero or less yields once."""
        ...


class SystemClock:
    """The real clock: ``time.perf_counter``, ``time.time`` and ``asyncio.sleep``."""

    def monotonic(self) -> float:
        return time.perf_counter()

    def wall(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(seconds, 0.0))


@dataclasses.dataclass(frozen=True, slots=True)
class TurnSpec:
    user_text: str
    max_tokens: int
    think_time_before_s: float


@dataclasses.dataclass(frozen=True, slots=True)
class Session:
    session_id: int
    turns: tuple[TurnSpec, ...]


@dataclasses.dataclass(slots=True)
class TurnResult:
    session_id: int
    turn_index: int  # 1-based
    ok: bool
    ttft_s: float | None = None
    completion_tokens: int | None = None
    latency_s: float | None = None
    error: str | None = None
    # Populated for the per-turn records file (see write_turn_records) and the
    # window/level computation below.
    send_ts_monotonic: float | None = None
    send_ts_wall: float | None = None
    first_token_ts_wall: float | None = None
    completion_ts_wall: float | None = None
    prompt_tokens: int | None = None
    cached_tokens: int | None = None
    # perf_counter timestamps of the first and last streamed token, used by
    # tokens_attributed_to_window / window_throughput. None whenever ttft_s is
    # None (no token was ever observed).
    first_token_ts_perf: float | None = None
    completion_ts_perf: float | None = None


@dataclasses.dataclass(slots=True)
class TurnAttempt:
    """Outcome of one POST to ``/v1/chat/completions``, before latency bookkeeping."""

    text: str
    ttft_s: float | None
    completion_tokens: int | None
    prompt_tokens: int | None
    cached_tokens: int | None
    first_token_ts_wall: float | None
    error: str | None


def make_paragraph(rng: random.Random) -> str:
    n_words = rng.randint(MIN_WORDS, MAX_WORDS)
    words = [rng.choice(WORD_BANK) for _ in range(n_words)]
    text = " ".join(words)
    return text[:1].upper() + text[1:] + "."


def make_session(seed: int, session_id: int) -> Session:
    """Deterministically build one session, independent of every other session_id.

    The per-session RNG is seeded from ``f"{seed}-{session_id}"``, so this is
    safe to call for any ``session_id`` on demand, in any order: it is what
    lets the ramp controller's session stream be unbounded (see
    ``_SessionPool``) rather than a fixed pre-generated list.
    """
    rng = random.Random(f"{seed}-{session_id}")
    n_turns = rng.randint(MIN_TURNS, MAX_TURNS)
    turns = []
    for turn_index in range(n_turns):
        think_time = 0.0 if turn_index == 0 else rng.uniform(THINK_TIME_MIN_S, THINK_TIME_MAX_S)
        turns.append(
            TurnSpec(
                user_text=make_paragraph(rng),
                max_tokens=rng.randint(MIN_MAX_TOKENS, MAX_MAX_TOKENS),
                think_time_before_s=think_time,
            )
        )
    return Session(session_id=session_id, turns=tuple(turns))


def generate_sessions(seed: int, n_sessions: int = N_SESSIONS) -> list[Session]:
    return [make_session(seed, session_id) for session_id in range(n_sessions)]


def parse_ramp_levels(raw: str | None) -> tuple[int, ...]:
    """Parse ``--ramp``'s comma-separated ints, or return the default schedule.

    ``raw is None`` means the flag was omitted: this is also what the
    framework's scored invocation always does (see ``ramp_default_schedule_complete``
    below). Raises ``argparse.ArgumentTypeError`` (a clean CLI error message)
    on an empty string, a non-integer entry, a non-positive level, or levels
    that are not strictly increasing (which also rejects duplicates): the
    ramp assumes concurrency only ever grows from level to level.
    """
    if raw is None:
        return DEFAULT_RAMP_LEVELS
    if not raw.strip():
        raise argparse.ArgumentTypeError("--ramp must not be empty")
    try:
        levels = tuple(int(part.strip()) for part in raw.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--ramp must be a comma-separated list of ints, got {raw!r}: {exc}"
        ) from exc
    if any(level <= 0 for level in levels):
        raise argparse.ArgumentTypeError(f"--ramp levels must all be positive, got {levels!r}")
    if any(earlier >= later for earlier, later in zip(levels, levels[1:], strict=False)):
        raise argparse.ArgumentTypeError(
            f"--ramp levels must be strictly increasing (no duplicates), got {levels!r}"
        )
    return levels


def tokens_attributed_to_window(
    first_token_ts: float,
    completion_ts: float,
    completion_tokens: int,
    window_start: float,
    window_end: float,
) -> float:
    """Fraction of one turn's completion tokens that fall inside a window.

    Tokens are attributed uniformly across ``[first_token_ts, completion_ts]``
    (rate = completion_tokens / duration). A turn whose interval has zero
    duration (``completion_ts <= first_token_ts``, e.g. a 1-token completion)
    is a point mass at ``first_token_ts``: it counts fully if that instant
    falls in ``[window_start, window_end)``, else not at all.
    """
    if completion_ts <= first_token_ts:
        return float(completion_tokens) if window_start <= first_token_ts < window_end else 0.0
    overlap_start = max(first_token_ts, window_start)
    overlap_end = min(completion_ts, window_end)
    overlap = overlap_end - overlap_start
    if overlap <= 0:
        return 0.0
    rate = completion_tokens / (completion_ts - first_token_ts)
    return rate * overlap


def window_throughput(
    turns: Iterable[tuple[float, float, int]], window_start: float, window_end: float
) -> float:
    """Aggregate completion-token throughput over ``[window_start, window_end)``.

    ``turns`` is an iterable of ``(first_token_ts, completion_ts,
    completion_tokens)``, one per successfully completed turn (regardless of
    when it was sent: a turn straddling the window boundary contributes its
    partial overlap via ``tokens_attributed_to_window``).
    """
    duration = window_end - window_start
    if duration <= 0:
        return 0.0
    total = sum(
        tokens_attributed_to_window(
            first_token_ts, completion_ts, completion_tokens, window_start, window_end
        )
        for first_token_ts, completion_ts, completion_tokens in turns
    )
    return total / duration


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    k = (len(ordered) - 1) * p / 100.0
    f, c = int(k), min(int(k) + 1, len(ordered) - 1)
    if f == c:
        return ordered[f]
    return ordered[f] * (c - k) + ordered[c] * (k - f)


async def send_chat_turn(
    client,
    base_url: str,
    messages: list[dict],
    max_tokens: int,
    clock: Clock,
) -> TurnAttempt:
    """POST one chat turn and report timing, tokens, and any error.

    The client bounds each request by ``REQUEST_TIMEOUT_S`` (``open_http_client``);
    a request that exceeds it is a failed turn.
    """
    body = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
        "ignore_eos": True,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    url = base_url.rstrip("/") + "/v1/chat/completions"
    started = clock.monotonic()
    first_token: float | None = None
    first_token_ts_wall: float | None = None
    parts: list[str] = []
    completion_tokens: int | None = None
    prompt_tokens: int | None = None
    cached_tokens: int | None = None
    try:
        async with client.post(url, json=body) as resp:
            if resp.status != 200:
                text = await resp.text()
                return TurnAttempt(
                    "",
                    None,
                    None,
                    None,
                    None,
                    None,
                    f"HTTP {resp.status}: {text[:500]}",
                )
            async for raw_line in resp.content:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                payload = line[len("data: ") :].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                usage = chunk.get("usage")
                if usage:
                    completion_tokens = usage.get("completion_tokens", completion_tokens)
                    prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                    cached_details = usage.get("prompt_tokens_details") or {}
                    cached_tokens = cached_details.get("cached_tokens", cached_tokens)
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    if first_token is None:
                        first_token = clock.monotonic()
                        first_token_ts_wall = clock.wall()
                    parts.append(content)
    except Exception as exc:
        ttft = first_token and (first_token - started)
        return TurnAttempt(
            "".join(parts),
            ttft,
            completion_tokens,
            prompt_tokens,
            cached_tokens,
            first_token_ts_wall,
            f"{type(exc).__name__}: {exc}",
        )
    ttft = None if first_token is None else first_token - started
    return TurnAttempt(
        "".join(parts),
        ttft,
        completion_tokens,
        prompt_tokens,
        cached_tokens,
        first_token_ts_wall,
        None,
    )


def validate_attempt(attempt: TurnAttempt, max_tokens: int) -> str | None:
    """Return why a transport-level success is still a failed turn, else None.

    Every turn has a fixed output budget the server must fill exactly
    (``ignore_eos``), so an empty reply, a missing usage report, or any
    completion token count other than ``max_tokens`` is a dropped or truncated
    response, never a fast one.
    """
    if not attempt.text:
        return "empty response"
    if attempt.completion_tokens is None:
        return "no usage.completion_tokens in the stream (send stream_options.include_usage)"
    if attempt.completion_tokens != max_tokens:
        return f"truncated: {attempt.completion_tokens} completion tokens, budget {max_tokens}"
    return None


# ---------------------------------------------------------------------------
# In-flight load controller: a shared session pool serviced by C persistent
# worker coroutines per the current ramp level.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(slots=True)
class _SessionState:
    session: Session
    next_turn_index: int
    history: list[dict]


class _SessionPool:
    """The shared pool of resting and not-yet-created sessions for one ramp run.

    A worker asks for work with ``take_work(now)``: the earliest-ready
    resting session if one's think time has already elapsed, else a
    brand-new session admitted from the unbounded ``make_session(seed, i)``
    stream. A worker never blocks waiting on a resting session's think time;
    it always has a fresh session to fall back to, which is what keeps a
    free worker from ever idling. This also makes a separate "pool size"
    hyperparameter unnecessary: resting sessions simply accumulate on their
    own once enough sessions are in flight for the server's own pace to
    start catching up with think time, so the pool self-sizes.
    """

    def __init__(self, seed: int) -> None:
        self._seed = seed
        self._next_new_index = 0
        self._resting: list[tuple[float, int, _SessionState]] = []
        self._heap_seq = 0

    def take_work(self, now: float) -> _SessionState:
        if self._resting and self._resting[0][0] <= now:
            _, _, state = heapq.heappop(self._resting)
            return state
        session = make_session(self._seed, self._next_new_index)
        self._next_new_index += 1
        return _SessionState(session=session, next_turn_index=0, history=[])

    def rest(self, state: _SessionState, ready_at: float) -> None:
        self._heap_seq += 1
        heapq.heappush(self._resting, (ready_at, self._heap_seq, state))


@dataclasses.dataclass(slots=True)
class _TurnOutcome:
    result: TurnResult
    history_with_user_turn: list[dict]
    assistant_text: str


async def _send_next_turn(
    client, base_url: str, state: _SessionState, clock: Clock
) -> _TurnOutcome:
    """Send ``state``'s next turn and report its outcome. Does not mutate ``state``."""
    turn = state.session.turns[state.next_turn_index]
    turn_index = state.next_turn_index + 1
    history = [*state.history, {"role": "user", "content": turn.user_text}]
    send_ts_wall = clock.wall()
    send_ts_monotonic = clock.monotonic()
    attempt = await send_chat_turn(client, base_url, history, turn.max_tokens, clock)
    latency_s = clock.monotonic() - send_ts_monotonic
    completion_ts_wall = clock.wall()
    error = attempt.error or validate_attempt(attempt, turn.max_tokens)
    ok = error is None
    first_token_ts_perf = None if attempt.ttft_s is None else send_ts_monotonic + attempt.ttft_s
    result = TurnResult(
        session_id=state.session.session_id,
        turn_index=turn_index,
        ok=ok,
        ttft_s=attempt.ttft_s,
        completion_tokens=attempt.completion_tokens,
        latency_s=latency_s,
        error=error,
        send_ts_monotonic=send_ts_monotonic,
        send_ts_wall=send_ts_wall,
        first_token_ts_wall=attempt.first_token_ts_wall,
        completion_ts_wall=completion_ts_wall,
        prompt_tokens=attempt.prompt_tokens,
        cached_tokens=attempt.cached_tokens,
        first_token_ts_perf=first_token_ts_perf,
        completion_ts_perf=send_ts_monotonic + latency_s,
    )
    return _TurnOutcome(result=result, history_with_user_turn=history, assistant_text=attempt.text)


@dataclasses.dataclass(slots=True)
class _WorkerContext:
    pool: _SessionPool
    client: object
    base_url: str
    all_results: list[TurnResult]
    stop_event: asyncio.Event
    clock: Clock


async def _worker_loop(ctx: _WorkerContext) -> None:
    """One persistent worker: forever take work, send it, retire or rest the session.

    A failed turn (``not result.ok``) ends its session -- it is simply not
    handed back to the pool -- but never raises: the worker loops back to
    ``take_work`` for its next piece of work, since ending the ramp on a
    failure is the coordinator's decision (see ``decide_level_outcome``), not
    an unhandled exception blowing up mid-measurement.
    """
    while not ctx.stop_event.is_set():
        state = ctx.pool.take_work(ctx.clock.monotonic())
        outcome = await _send_next_turn(ctx.client, ctx.base_url, state, ctx.clock)
        ctx.all_results.append(outcome.result)
        if not outcome.result.ok:
            continue  # session retired.
        state.history = [
            *outcome.history_with_user_turn,
            {"role": "assistant", "content": outcome.assistant_text},
        ]
        state.next_turn_index += 1
        if state.next_turn_index >= len(state.session.turns):
            continue  # session finished all its turns; retired.
        next_turn = state.session.turns[state.next_turn_index]
        ctx.pool.rest(state, ctx.clock.monotonic() + next_turn.think_time_before_s)


def _spawn_additional_workers(
    workers: list[asyncio.Task], current: int, target: int, ctx: _WorkerContext
) -> int:
    for _ in range(target - current):
        workers.append(asyncio.create_task(_worker_loop(ctx)))
    return target


async def _stop_workers(stop_event: asyncio.Event, workers: list[asyncio.Task]) -> None:
    stop_event.set()
    await asyncio.gather(*workers, return_exceptions=True)


async def _sleep_until(clock: Clock, target: float) -> None:
    delay = target - clock.monotonic()
    if delay > 0:
        await clock.sleep(delay)


# ---------------------------------------------------------------------------
# Ramp levels: window measurement, extension, validity, and the stop rule.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class _Window:
    start: float
    end: float


@dataclasses.dataclass(frozen=True, slots=True)
class LevelMetrics:
    concurrency: int
    level_start: float
    window_start: float
    window_end: float
    num_turns_sent_in_window: int
    throughput_tok_s: float
    ttft_p50_ms: float
    ttft_p95_ms: float
    ttft_p99_ms: float
    ttft_turn2plus_p50_ms: float
    ttft_turn2plus_p95_ms: float
    ttft_turn2plus_p99_ms: float
    tpot_p50_ms: float
    tpot_p95_ms: float
    low_sample: bool
    valid: bool
    invalid_reason: str | None
    # Set (via dataclasses.replace) only by _run_level_window's fail-fast
    # check under RampConfig.early_abort_enabled; never set on the scored path.
    early_aborted: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class RampConfig:
    warmup_s: float
    window_s: float
    extension_cap_s: float
    ref_concurrency: int
    guardrail_tpot_p95_ms: float
    guardrail_ttft_p95_ms: float
    early_stop_disabled: bool
    # Quick mode's mid-window fail-fast check (see _early_abort_breach). False
    # on the scored path always: a guardrail breach there is still reported
    # only once the full window (or its extension) has run.
    early_abort_enabled: bool = False


def ramp_config_from_args(args: argparse.Namespace) -> RampConfig:
    """Build the level-independent ramp configuration, resolving --quick's defaults.

    ``--warmup-s``/``--window-s``/``--window-extension-cap-s`` all take an
    explicit flag value first; --quick only supplies its own default when the
    flag was omitted (argparse default ``None`` for the first two -- see
    ``main``). This is what lets ``--quick --warmup-s 5`` still work: --quick
    is a preset, not a flag override.
    """
    warmup_s = (
        args.warmup_s
        if args.warmup_s is not None
        else (QUICK_WARMUP_S if args.quick else DEFAULT_WARMUP_S)
    )
    window_s = (
        args.window_s
        if args.window_s is not None
        else (QUICK_WINDOW_S if args.quick else DEFAULT_WINDOW_S)
    )
    if args.window_extension_cap_s is not None:
        extension_cap_s = args.window_extension_cap_s
    elif args.quick:
        extension_cap_s = QUICK_WINDOW_EXTENSION_MULTIPLIER * window_s
    else:
        extension_cap_s = DEFAULT_WINDOW_EXTENSION_MULTIPLIER * window_s
    return RampConfig(
        warmup_s=warmup_s,
        window_s=window_s,
        extension_cap_s=extension_cap_s,
        ref_concurrency=args.ref_concurrency,
        guardrail_tpot_p95_ms=args.guardrail_tpot_p95_ms,
        guardrail_ttft_p95_ms=args.guardrail_ttft_p95_ms,
        early_stop_disabled=args.no_early_stop,
        # --no-early-stop's documented contract ("running every requested
        # level regardless") also covers this mid-window check.
        early_abort_enabled=args.quick and not args.no_early_stop,
    )


def resolve_ramp_levels(args: argparse.Namespace) -> tuple[tuple[int, ...], bool]:
    """Resolve the ramp schedule and whether it is the framework's exact scored default.

    ``--ramp`` always wins when given. Otherwise ``--quick`` uses its own
    single-level preset (``QUICK_RAMP_LEVELS``, C=48); without either, the
    default schedule (``DEFAULT_RAMP_LEVELS``) is what the framework's scored
    invocation always uses. ``used_default_schedule`` is True only for that
    last case: --quick's preset is likewise an implicit default, but it is
    never the scored schedule, so ``ramp_default_schedule_complete`` (see
    ``aggregate_ramp_metrics``) must not read it as one.
    """
    if args.ramp is not None:
        return args.ramp, False
    if args.quick:
        return QUICK_RAMP_LEVELS, False
    return DEFAULT_RAMP_LEVELS, True


def _turns_in_range(results: list[TurnResult], start: float, end: float) -> list[TurnResult]:
    return [
        r for r in results if r.send_ts_monotonic is not None and start <= r.send_ts_monotonic < end
    ]


def _level_invalid_reason(level_results: list[TurnResult]) -> str | None:
    """Why a level is invalid: a failed/dropped/empty/short/errored turn, and nothing else.

    A sample shortfall after the window's extension cap is NOT invalidity: a
    genuinely slow server cannot produce more samples in the same wall time,
    and a slow server must score low, not error. See ``compute_level_metrics``'s
    ``low_sample`` flag, which is what reports that condition instead.
    """
    failed = [r for r in level_results if not r.ok]
    if failed:
        return f"{len(failed)} failed/dropped/empty/short turn(s) during the level"
    return None


def _throughput_inputs(results: list[TurnResult]) -> Iterable[tuple[float, float, int]]:
    for r in results:
        if (
            r.ok
            and r.first_token_ts_perf is not None
            and r.completion_ts_perf is not None
            and r.completion_tokens is not None
        ):
            yield (r.first_token_ts_perf, r.completion_ts_perf, r.completion_tokens)


def _ttft_ms_values(window_results: list[TurnResult], min_turn_index: int = 1) -> list[float]:
    return [
        r.ttft_s * 1000
        for r in window_results
        if r.ok and r.turn_index >= min_turn_index and r.ttft_s is not None
    ]


def _tpot_ms_values(window_results: list[TurnResult]) -> list[float]:
    return [
        (r.latency_s - r.ttft_s) / (r.completion_tokens - 1) * 1000
        for r in window_results
        if r.ok
        and r.ttft_s is not None
        and r.latency_s is not None
        and r.completion_tokens
        and r.completion_tokens > 1
    ]


def compute_level_metrics(
    all_results: list[TurnResult],
    level_start: float,
    window: _Window,
    concurrency: int,
    min_turns: int,
) -> LevelMetrics:
    """Reduce one level's turns to its reported metrics, its ``low_sample`` flag, and its validity.

    ``window_results`` (turns SENT in ``[window.start, window.end)``) drives
    the latency percentiles and the ``low_sample`` flag (fewer than
    ``min_turns`` turns sent even after the window's extension, e.g. a
    genuinely slow server at low concurrency). ``low_sample`` does NOT make
    the level invalid: it is still measured, still eligible for
    ``peak_goodput_tok_s``, and its latency percentiles are still reported,
    just flagged as coming from fewer samples than usual. ``level_results``
    (turns SENT anywhere in ``[level_start, window.end)``, i.e. including this
    level's warmup) drives the only thing that does make a level invalid: a
    failed/dropped/empty/short/errored turn (a server that only breaks during
    warmup is still broken). Throughput is computed over every successfully
    completed turn regardless of when it was sent, since a turn's activity
    interval can straddle the window boundary (see ``window_throughput``).
    """
    window_results = _turns_in_range(all_results, window.start, window.end)
    level_results = _turns_in_range(all_results, level_start, window.end)
    invalid_reason = _level_invalid_reason(level_results)
    low_sample = len(window_results) < min_turns
    throughput = window_throughput(_throughput_inputs(all_results), window.start, window.end)
    ttft_all = _ttft_ms_values(window_results)
    ttft_turn2plus = _ttft_ms_values(window_results, min_turn_index=2)
    tpot = _tpot_ms_values(window_results)
    return LevelMetrics(
        concurrency=concurrency,
        level_start=level_start,
        window_start=window.start,
        window_end=window.end,
        num_turns_sent_in_window=len(window_results),
        throughput_tok_s=throughput,
        ttft_p50_ms=percentile(ttft_all, 50),
        ttft_p95_ms=percentile(ttft_all, 95),
        ttft_p99_ms=percentile(ttft_all, 99),
        ttft_turn2plus_p50_ms=percentile(ttft_turn2plus, 50),
        ttft_turn2plus_p95_ms=percentile(ttft_turn2plus, 95),
        ttft_turn2plus_p99_ms=percentile(ttft_turn2plus, 99),
        tpot_p50_ms=percentile(tpot, 50),
        tpot_p95_ms=percentile(tpot, 95),
        low_sample=low_sample,
        valid=invalid_reason is None,
        invalid_reason=invalid_reason,
    )


def _count_turns_sent(results: list[TurnResult], window_start: float, window_end: float) -> int:
    return sum(
        1
        for r in results
        if r.send_ts_monotonic is not None and window_start <= r.send_ts_monotonic < window_end
    )


def _early_abort_breach(window_results: list[TurnResult], config: RampConfig) -> bool:
    """Whether the level's p95 TPOT has already blown past the fail-fast threshold.

    Requires at least ``EARLY_ABORT_MIN_TURNS`` completed turns so a handful
    of slow turns at the very start of a level (server/connection ramp-up)
    cannot trip a false abort. Only ever consulted when
    ``config.early_abort_enabled`` (quick mode); the scored path's guardrail
    check (``decide_level_outcome``) is unaffected and still runs against the
    full window.
    """
    tpot = _tpot_ms_values(window_results)
    if len(tpot) < EARLY_ABORT_MIN_TURNS:
        return False
    return percentile(tpot, 95) > EARLY_ABORT_GUARDRAIL_MULTIPLIER * config.guardrail_tpot_p95_ms


async def _wait_for_window(
    ctx: _WorkerContext, window_start: float, window_end: float, config: RampConfig
) -> tuple[float, bool]:
    """Sleep until ``window_end``, or until an early-abort breach cuts it short.

    Returns the actual (possibly truncated) window end and whether it was
    truncated. Without ``config.early_abort_enabled`` this is exactly one
    sleep to ``window_end``, unchanged from before this check existed; with
    it, wakes every ``EARLY_ABORT_POLL_INTERVAL_S`` to check
    ``_early_abort_breach`` against the turns seen so far.
    """
    clock = ctx.clock
    if not config.early_abort_enabled:
        await _sleep_until(clock, window_end)
        return window_end, False
    poll_interval = min(EARLY_ABORT_POLL_INTERVAL_S, config.window_s)
    while True:
        now = clock.monotonic()
        if now >= window_end:
            return window_end, False
        await _sleep_until(clock, min(now + poll_interval, window_end))
        now = clock.monotonic()
        window_results = _turns_in_range(ctx.all_results, window_start, now)
        if _early_abort_breach(window_results, config):
            return now, True


def _report_early_abort(concurrency: int, level: LevelMetrics, config: RampConfig) -> None:
    """Print a clear, non-silent notice that a level's window was cut short."""
    print(
        f"level C={concurrency}: p95 TPOT ({level.tpot_p95_ms:.0f}ms) breached "
        f"{EARLY_ABORT_GUARDRAIL_MULTIPLIER:.0f}x the {config.guardrail_tpot_p95_ms:.0f}ms guardrail "
        f"after {level.num_turns_sent_in_window} turns; aborting this level's measurement early "
        "(--quick) instead of waiting out the rest of the window",
        file=sys.stderr,
    )


async def _run_level_window(
    ctx: _WorkerContext, level_start: float, config: RampConfig, concurrency: int
) -> LevelMetrics:
    """Run one level's warmup + measurement window (extending or aborting it as needed).

    ``[level_start, level_start + warmup_s)`` is warmup, excluded from
    measurement. The window then collects for ``window_s``; if fewer than
    ``max(20, 2*concurrency)`` turns were sent in it, the window is extended
    in ``window_s`` increments up to ``config.extension_cap_s`` total. If the
    cap is hit and still under the minimum, the window stops extending there;
    ``compute_level_metrics`` flags the level ``low_sample`` rather than
    invalid (a slow server must score low, not error) and it is still
    measured, still eligible for ``peak_goodput_tok_s``, and its latency
    percentiles are still reported, just noisier than usual.

    Under ``config.early_abort_enabled`` (quick mode), ``_wait_for_window``
    may instead cut the window short partway through on a clear guardrail
    breach (see ``_early_abort_breach``); the window is never extended in
    that case (there is nothing to wait for) and the breach is reported via
    ``_report_early_abort``, not left as a silent truncation.
    """
    all_results = ctx.all_results
    window_start = level_start + config.warmup_s
    window_end, aborted = await _wait_for_window(
        ctx, window_start, window_start + config.window_s, config
    )
    min_turns = max(20, 2 * concurrency)
    while not aborted and _count_turns_sent(all_results, window_start, window_end) < min_turns:
        elapsed = window_end - window_start
        if elapsed >= config.extension_cap_s:
            break
        window_end += min(config.window_s, config.extension_cap_s - elapsed)
        await _sleep_until(ctx.clock, window_end)
    level = compute_level_metrics(
        all_results, level_start, _Window(window_start, window_end), concurrency, min_turns
    )
    if aborted:
        level = dataclasses.replace(level, early_aborted=True)
        _report_early_abort(concurrency, level, config)
    return level


@dataclasses.dataclass(frozen=True, slots=True)
class _RampProgress:
    best_goodput: float | None
    consecutive_low_gain: int


@dataclasses.dataclass(frozen=True, slots=True)
class _LevelDecision:
    action: Literal["continue", "stop", "hard_error"]
    progress: _RampProgress


def _decide_plateau(
    level: LevelMetrics, config: RampConfig, progress: _RampProgress
) -> _LevelDecision:
    if progress.best_goodput is None:
        return _LevelDecision("continue", _RampProgress(level.throughput_tok_s, 0))
    best = progress.best_goodput
    gain = (level.throughput_tok_s - best) / best if best > 0 else float("inf")
    consecutive = progress.consecutive_low_gain + 1 if gain < PLATEAU_GAIN_THRESHOLD else 0
    new_progress = _RampProgress(max(best, level.throughput_tok_s), consecutive)
    if consecutive >= PLATEAU_CONSECUTIVE_LEVELS and not config.early_stop_disabled:
        return _LevelDecision("stop", new_progress)
    return _LevelDecision("continue", new_progress)


def decide_level_outcome(
    level: LevelMetrics, *, is_first_level: bool, config: RampConfig, progress: _RampProgress
) -> _LevelDecision:
    """Decide what a just-measured level means for the ramp: continue, stop, or hard-error.

    An invalid level (any failed/dropped/empty/short/errored turn during it --
    a sample shortfall after the window's extension cap is NOT invalidity, see
    ``compute_level_metrics``'s ``low_sample`` flag) always ends the ramp; if
    it is the first level or the reference-concurrency level (``C_REF``), the
    whole run is a hard error instead, exactly like today's whole-run
    all-or-nothing rule. A guardrail breach (TPOT or turn2+ TTFT p95 over the configured
    threshold) stops the ramp unless ``--no-early-stop`` was passed, in which
    case the ramp keeps going but this level's throughput is excluded from
    goodput eligibility (``progress.best_goodput`` is left unchanged). A
    two-consecutive-level throughput plateau (gain under
    ``PLATEAU_GAIN_THRESHOLD``) also stops the ramp unless
    ``--no-early-stop`` was passed.
    """
    if not level.valid:
        if is_first_level or level.concurrency == config.ref_concurrency:
            return _LevelDecision("hard_error", progress)
        return _LevelDecision("stop", progress)
    breaches = (
        level.ttft_turn2plus_p95_ms > config.guardrail_ttft_p95_ms
        or level.tpot_p95_ms > config.guardrail_tpot_p95_ms
    )
    if breaches:
        action: Literal["continue", "stop"] = "continue" if config.early_stop_disabled else "stop"
        return _LevelDecision(action, progress)
    return _decide_plateau(level, config, progress)


def resolve_peak_goodput(
    levels_run: list[LevelMetrics], best_goodput: float | None
) -> float | None:
    """``peak_goodput_tok_s``: the running max throughput among guardrail-eligible levels.

    Falls back to the max throughput of any valid level when no level ever
    qualified (every level breached the guardrail immediately, including
    level 1): guardrail-eligibility is meant to exclude degenerate-queueing
    levels from being called "peak", not to make the metric disappear
    entirely when nothing else is available. Returns None only when no valid
    level ran at all, which cannot happen once the caller has returned
    normally (an invalid first level is always a hard error, see
    ``decide_level_outcome``).
    """
    if best_goodput is not None:
        return best_goodput
    valid_throughputs = [lvl.throughput_tok_s for lvl in levels_run if lvl.valid]
    return max(valid_throughputs) if valid_throughputs else None


def _finalize_ramp_levels(
    provisional_levels: list[LevelMetrics],
    all_results: list[TurnResult],
    config: RampConfig,
) -> tuple[list[LevelMetrics], float | None]:
    """Recompute reported levels after worker shutdown has drained active turns.

    A level snapshot taken at ``window_end`` cannot contain a request that was
    active across that boundary: the worker appends its ``TurnResult`` only
    when the request completes. ``_stop_workers`` waits for those requests,
    so rebuilding from the drained result list includes their token overlap,
    latency, and validity in the final report.

    Decisions to continue or stop the live ramp necessarily use the
    provisional snapshots. Waiting for each boundary-crossing request before
    deciding would reduce concurrency and change the workload. Only the
    finalized levels and their guardrail-eligible maximum are reported.
    """
    finalized: list[LevelMetrics] = []
    eligible_goodput: list[float] = []
    for provisional in provisional_levels:
        level = compute_level_metrics(
            all_results,
            provisional.level_start,
            _Window(provisional.window_start, provisional.window_end),
            provisional.concurrency,
            max(20, 2 * provisional.concurrency),
        )
        level = dataclasses.replace(level, early_aborted=provisional.early_aborted)
        finalized.append(level)
        breaches = (
            level.ttft_turn2plus_p95_ms > config.guardrail_ttft_p95_ms
            or level.tpot_p95_ms > config.guardrail_tpot_p95_ms
        )
        if level.valid and not breaches:
            eligible_goodput.append(level.throughput_tok_s)
    return finalized, max(eligible_goodput, default=None)


def _hard_error_message(level: LevelMetrics, level_index: int, config: RampConfig) -> str:
    reason = (
        "the first ramp level"
        if level_index == 0
        else f"the reference concurrency C_REF={config.ref_concurrency}"
    )
    return (
        f"ramp level C={level.concurrency} is invalid ({level.invalid_reason}); "
        f"this is {reason}, so the whole run fails."
    )


@dataclasses.dataclass(slots=True)
class RampRun:
    levels_run: list[LevelMetrics]
    all_results: list[TurnResult]
    best_goodput: float | None
    wall_s: float


def open_http_client() -> AbstractAsyncContextManager:
    """The real HTTP client: one ``aiohttp.ClientSession`` for the whole ramp.

    Every request it sends is bounded by ``REQUEST_TIMEOUT_S`` in total.
    """
    # lint-waiver: LW-018703 [PLC0415]; aiohttp is imported where the real client is opened.
    # > A module-level import would make every helper, and the tests of the pure
    # > metric code, need aiohttp installed; nothing else in the module uses it.
    import aiohttp  # noqa: PLC0415

    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S))


async def run_ramp(
    base_url: str,
    seed: int,
    levels: tuple[int, ...],
    config: RampConfig,
    *,
    clock: Clock,
    open_client: Callable[[], AbstractAsyncContextManager] = open_http_client,
) -> RampRun:
    """Run the concurrency ramp to completion (or an early stop).

    One continuous run: the session pool and turn-record sink are shared for
    the whole ramp's lifetime. Concurrency only increases across levels
    (levels run in increasing order), so a level transition simply spawns
    ``(C_next - C_prev)`` additional persistent worker coroutines; workers
    are never torn down until the whole ramp ends. ``clock`` and
    ``open_client`` are the seams a test drives the ramp through.
    """
    all_results: list[TurnResult] = []
    stop_event = asyncio.Event()
    workers: list[asyncio.Task] = []
    levels_run: list[LevelMetrics] = []
    progress = _RampProgress(best_goodput=None, consecutive_low_gain=0)
    current_concurrency = 0
    wall_started = clock.monotonic()

    async with open_client() as client:
        ctx = _WorkerContext(
            pool=_SessionPool(seed),
            client=client,
            base_url=base_url,
            all_results=all_results,
            stop_event=stop_event,
            clock=clock,
        )
        for level_index, target_concurrency in enumerate(levels):
            current_concurrency = _spawn_additional_workers(
                workers, current_concurrency, target_concurrency, ctx
            )
            level = await _run_level_window(ctx, clock.monotonic(), config, target_concurrency)
            levels_run.append(level)
            decision = decide_level_outcome(
                level, is_first_level=level_index == 0, config=config, progress=progress
            )
            progress = decision.progress
            if decision.action == "hard_error":
                await _stop_workers(stop_event, workers)
                raise RuntimeError(_hard_error_message(level, level_index, config))
            if decision.action == "stop":
                break
        await _stop_workers(stop_event, workers)

    levels_run, best_goodput = _finalize_ramp_levels(levels_run, all_results, config)
    for level_index, level in enumerate(levels_run):
        if not level.valid and (level_index == 0 or level.concurrency == config.ref_concurrency):
            raise RuntimeError(_hard_error_message(level, level_index, config))

    return RampRun(
        levels_run=levels_run,
        all_results=all_results,
        best_goodput=best_goodput,
        wall_s=clock.monotonic() - wall_started,
    )


# ---------------------------------------------------------------------------
# Metric schema, aggregation, and the protocol/JSON reporting split.
# ---------------------------------------------------------------------------


def level_metric_names(concurrency: int) -> tuple[str, str, str]:
    """The per-level metrics of one ramp level: throughput, turn-2+ p95 TTFT, p95 TPOT."""
    return (
        f"throughput_tok_s_c{concurrency}",
        f"p95_ttft_turn2plus_ms_c{concurrency}",
        f"p95_tpot_ms_c{concurrency}",
    )


def build_metric_specs(levels: tuple[int, ...], ref_concurrency: int) -> dict[str, MetricSpec]:
    """The metrics this benchmark declares in its ``hello`` record, for one schedule.

    ``p95_ttft_turn2plus_ms_at_ref``'s ``required`` bit varies by invocation:
    it is required iff ``ref_concurrency`` is one of the resolved ``--ramp``
    levels (known before measuring, since ``levels`` is a pure function of the
    CLI args). ``peak_goodput_tok_s`` stays required in every invocation (see
    ``resolve_peak_goodput``'s fallback). Each level also declares its own
    throughput and latency (``level_metric_names``), reported only when the
    level ran and was valid, so they are never required.
    """
    per_level: dict[str, MetricSpec] = {}
    for concurrency in levels:
        throughput, ttft, tpot = level_metric_names(concurrency)
        per_level[throughput] = MetricSpec(unit="tok/s", direction="max", required=False)
        per_level[ttft] = MetricSpec(unit="ms", direction="min", required=False)
        per_level[tpot] = MetricSpec(unit="ms", direction="min", required=False)
    return {
        "peak_goodput_tok_s": MetricSpec(unit="tok/s", direction="max"),
        "p95_ttft_turn2plus_ms_at_ref": MetricSpec(
            unit="ms", direction="min", required=ref_concurrency in levels
        ),
        "benchmark_version": MetricSpec(),
        "ramp_peak_level": MetricSpec(unit="sessions"),
        "ramp_num_levels_run": MetricSpec(unit="levels"),
        "ramp_default_schedule_complete": MetricSpec(),
        "num_completed_turns": MetricSpec(unit="turns", direction="max"),
        "num_failed_turns": MetricSpec(unit="turns", direction="min"),
        "wall_duration_s": MetricSpec(unit="s"),
        "guardrail_tpot_p95_ms": MetricSpec(unit="ms"),
        "guardrail_ttft_p95_ms": MetricSpec(unit="ms"),
        "ref_concurrency": MetricSpec(unit="sessions"),
        **per_level,
    }


# The scored schema (the default schedule, which holds C_REF), used for static
# introspection (declared-metrics tests) and as what the framework's scored
# invocation declares, since it always uses the default ramp schedule.
METRIC_SPECS: dict[str, MetricSpec] = build_metric_specs(DEFAULT_RAMP_LEVELS, C_REF)
METRICS: tuple[str, ...] = tuple(METRIC_SPECS)


def aggregate_ramp_metrics(
    run_result: RampRun, config: RampConfig, used_default_schedule: bool
) -> dict[str, float]:
    """Reduce a completed (or early-stopped) ramp run to the reported metric row.

    ``p95_ttft_turn2plus_ms_at_ref`` is present only when the reference
    concurrency level actually ran and was valid; otherwise the key is left
    out entirely (see ``_report_outcome`` for how that is turned into
    ``report.fail`` instead of ``report.emit`` when the metric is required).
    """
    valid_levels = [lvl for lvl in run_result.levels_run if lvl.valid]
    peak_goodput = resolve_peak_goodput(run_result.levels_run, run_result.best_goodput)
    ref_level = next(
        (lvl for lvl in valid_levels if lvl.concurrency == config.ref_concurrency), None
    )
    ok_results = [r for r in run_result.all_results if r.ok]
    failed_results = [r for r in run_result.all_results if not r.ok]
    ramp_default_schedule_complete = used_default_schedule and ref_level is not None

    values: dict[str, float] = {
        "peak_goodput_tok_s": peak_goodput if peak_goodput is not None else float("nan"),
        "benchmark_version": float(BENCHMARK_VERSION),
        "ramp_peak_level": float(max((lvl.concurrency for lvl in valid_levels), default=0)),
        "ramp_num_levels_run": float(len(run_result.levels_run)),
        "ramp_default_schedule_complete": 1.0 if ramp_default_schedule_complete else 0.0,
        "num_completed_turns": float(len(ok_results)),
        "num_failed_turns": float(len(failed_results)),
        "wall_duration_s": run_result.wall_s,
        "guardrail_tpot_p95_ms": config.guardrail_tpot_p95_ms,
        "guardrail_ttft_p95_ms": config.guardrail_ttft_p95_ms,
        "ref_concurrency": float(config.ref_concurrency),
    }
    if ref_level is not None:
        values["p95_ttft_turn2plus_ms_at_ref"] = ref_level.ttft_turn2plus_p95_ms
    for level in valid_levels:
        throughput, ttft, tpot = level_metric_names(level.concurrency)
        values[throughput] = level.throughput_tok_s
        values[ttft] = level.ttft_turn2plus_p95_ms
        values[tpot] = level.tpot_p95_ms
    return values


@dataclasses.dataclass(slots=True)
class MeasurementOutcome:
    values: dict[str, float]
    all_results: list[TurnResult]
    levels_run: list[LevelMetrics]
    ref_required: bool
    ref_reached: bool
    used_default_schedule: bool
    # False for a --quick run: it is a fast, non-scoring preset for the
    # optimization loop's inner iterations, never the framework's scored
    # invocation (see build_output_json_payload's "scoring" field).
    scoring: bool = True


def build_measurement_outcome(
    run_result: RampRun,
    levels: tuple[int, ...],
    config: RampConfig,
    used_default_schedule: bool,
    *,
    scoring: bool = True,
) -> MeasurementOutcome:
    values = aggregate_ramp_metrics(run_result, config, used_default_schedule)
    ref_required = config.ref_concurrency in levels
    ref_reached = any(
        lvl.valid and lvl.concurrency == config.ref_concurrency for lvl in run_result.levels_run
    )
    return MeasurementOutcome(
        values=values,
        all_results=run_result.all_results,
        levels_run=run_result.levels_run,
        ref_required=ref_required,
        ref_reached=ref_reached,
        used_default_schedule=used_default_schedule,
        scoring=scoring,
    )


def level_to_dict(level: LevelMetrics, config: RampConfig) -> dict[str, object]:
    breaches = (
        level.ttft_turn2plus_p95_ms > config.guardrail_ttft_p95_ms
        or level.tpot_p95_ms > config.guardrail_tpot_p95_ms
    )
    return {
        "concurrency": level.concurrency,
        "level_start": level.level_start,
        "window_start": level.window_start,
        "window_end": level.window_end,
        "num_turns_sent_in_window": level.num_turns_sent_in_window,
        "throughput_tok_s": level.throughput_tok_s,
        "ttft_p50_ms": level.ttft_p50_ms,
        "ttft_p95_ms": level.ttft_p95_ms,
        "ttft_p99_ms": level.ttft_p99_ms,
        "ttft_turn2plus_p50_ms": level.ttft_turn2plus_p50_ms,
        "ttft_turn2plus_p95_ms": level.ttft_turn2plus_p95_ms,
        "ttft_turn2plus_p99_ms": level.ttft_turn2plus_p99_ms,
        "tpot_p50_ms": level.tpot_p50_ms,
        "tpot_p95_ms": level.tpot_p95_ms,
        "low_sample": level.low_sample,
        "valid": level.valid,
        "invalid_reason": level.invalid_reason,
        "within_guardrail": not breaches,
        "early_aborted": level.early_aborted,
    }


def build_output_json_payload(outcome: MeasurementOutcome, config: RampConfig) -> dict[str, object]:
    """The full ``--output-json`` payload: the metric row plus the per-level table.

    Always buildable once a ``MeasurementOutcome`` exists (i.e. whenever
    measurement completed without raising), regardless of whether the
    protocol stream gets ``emit`` or ``fail`` -- this file is for humans and
    agents, not gated by protocol validity. ``partial`` is true exactly when
    the declared-required ``p95_ttft_turn2plus_ms_at_ref`` objective could not
    be reported this run. ``scoring`` is false exactly for a --quick run: an
    agent driving the optimization loop must not mistake a fast iteration
    measurement for a scored/acceptance one (see OBJECTIVE.md and the
    bundle's README "Iteration workflow" section).
    """
    payload: dict[str, object] = dict(outcome.values)
    payload["ramp_default_schedule_complete"] = (
        outcome.used_default_schedule and outcome.ref_reached
    )
    payload["partial"] = outcome.ref_required and not outcome.ref_reached
    payload["scoring"] = outcome.scoring
    payload["ramp_levels_run"] = [level_to_dict(lvl, config) for lvl in outcome.levels_run]
    return payload


def turns_output_path(output_json: Path) -> Path:
    """Sibling path for per-turn records, next to the ``--output-json`` file."""
    return output_json.with_name(output_json.name + ".turns.jsonl")


def metrics_output_path(output_json: str | None, vs_output: str | None) -> Path:
    """Resolve where the human-readable metrics JSON goes.

    ``--output-json`` wins when given. Otherwise the framework invoked this
    benchmark with only ``--vs-output`` (the result protocol declares that one
    flag, not this one), and the metrics JSON plus its per-turn records land
    beside the record stream so the diagnostics a failed round needs still
    exist. ``main`` rejects the case where neither flag is present.
    """
    if output_json is not None:
        return Path(output_json)
    assert vs_output is not None
    return Path(vs_output + ".metrics.json")


def write_metrics(path: Path, payload: dict[str, object]) -> None:
    """Write the metrics (plus, for --output-json, the per-level table) as flat JSON."""
    path.write_text(json.dumps(payload, indent=2) + "\n")


def write_turn_records(path: Path, results: list[TurnResult]) -> None:
    """Write one JSON line per attempted turn, for post-hoc tail/correlation analysis."""
    with path.open("w") as handle:
        for result in results:
            record = {
                "session_id": result.session_id,
                "turn_index": result.turn_index,
                "ok": result.ok,
                "send_ts_monotonic": result.send_ts_monotonic,
                "send_ts_wall": result.send_ts_wall,
                "ttft_ms": None if result.ttft_s is None else result.ttft_s * 1000,
                "first_token_ts_wall": result.first_token_ts_wall,
                "completion_ts_wall": result.completion_ts_wall,
                "output_tokens": result.completion_tokens,
                "prompt_tokens": result.prompt_tokens,
                "cached_tokens": result.cached_tokens,
                "error": result.error,
            }
            handle.write(json.dumps(record) + "\n")


def summary_line(values: dict[str, float]) -> str:
    """One-line stdout digest of a completed run."""
    ttft_at_ref = values.get("p95_ttft_turn2plus_ms_at_ref", float("nan"))
    return (
        f"peak_goodput_tok_s={values['peak_goodput_tok_s']:.1f} "
        f"p95_ttft_turn2plus_ms_at_ref={ttft_at_ref:.1f} "
        f"ramp_peak_level={values['ramp_peak_level']:.0f} "
        f"ramp_num_levels_run={values['ramp_num_levels_run']:.0f} "
        f"ramp_default_schedule_complete={values['ramp_default_schedule_complete']:.0f} "
        f"num_completed_turns={values['num_completed_turns']:.0f} "
        f"num_failed_turns={values['num_failed_turns']:.0f} "
        f"wall_duration_s={values['wall_duration_s']:.1f}"
    )


RampRunner = Callable[[str, int, tuple[int, ...], RampConfig], Awaitable[RampRun]]
"""Runs the ramp against a base URL: ``run_ramp`` on the system clock, or a test's Fake."""


async def run_ramp_on_system_clock(
    base_url: str, seed: int, levels: tuple[int, ...], config: RampConfig
) -> RampRun:
    """The scored ramp: real time and a real HTTP client."""
    return await run_ramp(base_url, seed, levels, config, clock=SystemClock())


async def measure(
    args: argparse.Namespace,
    levels: tuple[int, ...],
    config: RampConfig,
    used_default_schedule: bool,
    ramp: RampRunner,
) -> MeasurementOutcome:
    """Run the ramp against the candidate's server and reduce it to a ``MeasurementOutcome``.

    Raises only for a hard-error condition (transport setup, or an invalid
    first/reference-concurrency level from ``run_ramp``) so ``main_async`` has
    one place to turn that into the protocol's ``error`` record. Every other
    outcome -- including a ramp that stopped early via the plateau or
    guardrail rule, possibly before ever reaching ``C_REF`` -- returns
    normally; see ``_report_outcome`` for how that is reported.
    """
    run_result = await ramp(args.base_url, SEED, levels, config)
    return build_measurement_outcome(
        run_result, levels, config, used_default_schedule, scoring=not args.quick
    )


def _report_outcome(
    report: vs_protocol.ProtocolReport,
    output_path: Path,
    outcome: MeasurementOutcome,
    config: RampConfig,
) -> int:
    """Write the human-readable JSON, then either ``emit`` or ``fail`` the protocol row.

    ``report.fail`` is used, not ``report.emit``, exactly when the declared-
    required ``p95_ttft_turn2plus_ms_at_ref`` objective is genuinely absent
    (the ramp stopped -- via the plateau or guardrail rule -- before ever
    reaching ``C_REF``, despite ``C_REF`` being one of the planned levels).
    This still exits 0 and still writes the JSON: the run measured something
    real, it just cannot report every metric its schema declared required.
    """
    write_metrics(output_path, build_output_json_payload(outcome, config))
    write_turn_records(turns_output_path(output_path), outcome.all_results)
    if outcome.ref_required and not outcome.ref_reached:
        stopped_at = max((lvl.concurrency for lvl in outcome.levels_run if lvl.valid), default=0)
        report.fail(
            f"ramp stopped at concurrency {stopped_at} before reaching the reference "
            f"concurrency C_REF={config.ref_concurrency}; the declared-required "
            "p95_ttft_turn2plus_ms_at_ref objective cannot be reported for this run "
            "(see the written --output-json for the levels that did run)"
        )
        return 0
    print(summary_line(outcome.values))
    report.emit(outcome.values)
    return 0


async def main_async(args: argparse.Namespace, ramp: RampRunner = run_ramp_on_system_clock) -> int:
    output_path = metrics_output_path(args.output_json, args.vs_output)
    levels, used_default_schedule = resolve_ramp_levels(args)
    config = ramp_config_from_args(args)
    metric_specs = build_metric_specs(levels, config.ref_concurrency)
    report = vs_protocol.ProtocolReport(Path(args.vs_output) if args.vs_output else None)
    with report:
        # Declare the schema before measuring, per PROTOCOL.md: a crash or a
        # harness timeout then leaves a stream with a schema and no outcome
        # ("it started and died"), which the framework reports differently from
        # an empty file ("this producer does not speak the protocol").
        report.declare(metric_specs)
        try:
            outcome = await measure(args, levels, config, used_default_schedule, ramp)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            print(f"benchmark failed: {message}", file=sys.stderr)
            output_path.unlink(missing_ok=True)
            report.fail(message)
            return 1
        return _report_outcome(report, output_path, outcome, config)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get(SERVICE_URL_ENV),
        help=(
            "The candidate's server. Defaults to $" + SERVICE_URL_ENV + ", which "
            "vibesys-cli sets for a task of two images."
        ),
    )
    parser.add_argument(
        "--output-json",
        default=artifact_path("benchmark-metrics.json"),
        help=(
            "Path the human-readable metrics JSON (plus the full per-level ramp "
            "table) is written to, whenever measurement completes without an "
            "exception; the per-turn records go beside it. Defaults to the job's "
            f"artifacts (${ARTIFACTS_DIR_ENV}/benchmark-metrics.json) under "
            "vibesys-cli, else '<--vs-output>.metrics.json'. At least one of "
            "--output-json and --vs-output is required."
        ),
    )
    parser.add_argument(
        vs_protocol.OUTPUT_FLAG,
        default=None,
        help=(
            "Path the VibeSys evaluator result protocol v"
            f"{vs_protocol.PROTOCOL_VERSION} record stream is written to. "
            "VibeSys appends this flag itself for a task declaring "
            "benchmark.result_protocol; it is what the framework scores."
        ),
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help=(
            "Fast, non-scoring preset for the optimization loop's inner "
            f"iterations: a single ramp level at C={QUICK_RAMP_LEVELS[0]} (unless "
            f"--ramp is also given), a {QUICK_WARMUP_S:.0f}s warmup, a "
            f"{QUICK_WINDOW_S:.0f}s measurement window with no extension, and a "
            "fail-fast abort if the level's guardrail is clearly breached "
            "partway through the window (see EARLY_ABORT_GUARDRAIL_MULTIPLIER / "
            "EARLY_ABORT_MIN_TURNS). Marks --output-json's payload "
            "'scoring: false'. Not for acceptance/scoring runs -- omit --quick "
            "and use the default schedule for those."
        ),
    )
    parser.add_argument(
        "--ramp",
        type=parse_ramp_levels,
        default=None,
        help=(
            "Comma-separated, strictly increasing concurrency levels, e.g. "
            "'1,4,16,64'. Omit for the default schedule "
            f"({','.join(str(level) for level in DEFAULT_RAMP_LEVELS)}), which is "
            "what the framework's scored invocation always uses (or --quick's "
            f"single-level {QUICK_RAMP_LEVELS} preset, if --quick is given); pass "
            "an explicit subset (with or without --quick) for other exploration."
        ),
    )
    parser.add_argument(
        "--warmup-s",
        type=float,
        default=None,
        help=f"Default: {DEFAULT_WARMUP_S:.0f}s, or {QUICK_WARMUP_S:.0f}s under --quick.",
    )
    parser.add_argument(
        "--window-s",
        type=float,
        default=None,
        help=f"Default: {DEFAULT_WINDOW_S:.0f}s, or {QUICK_WINDOW_S:.0f}s under --quick.",
    )
    parser.add_argument(
        "--window-extension-cap-s",
        type=float,
        default=None,
        help=(
            "Max total window duration (after warmup) a level's measurement "
            f"window may extend to. Defaults to {DEFAULT_WINDOW_EXTENSION_MULTIPLIER:.0f}x "
            "--window-s, or exactly 1x --window-s (no extension) under --quick."
        ),
    )
    parser.add_argument("--ref-concurrency", type=int, default=C_REF)
    parser.add_argument("--guardrail-tpot-p95-ms", type=float, default=GUARDRAIL_TPOT_P95_MS)
    parser.add_argument(
        "--guardrail-ttft-p95-ms", type=float, default=GUARDRAIL_TTFT_TURN2PLUS_P95_MS
    )
    parser.add_argument(
        "--no-early-stop",
        action="store_true",
        help=(
            "Disable the plateau and guardrail early terminations, running every "
            "requested level regardless (for exploration). Also disables --quick's "
            "mid-window fail-fast abort. Does not disable the invalid-level rule."
        ),
    )
    args = parser.parse_args()
    if args.output_json is None and args.vs_output is None:
        parser.error("one of --output-json and --vs-output is required")
    if not args.base_url:
        parser.error(f"--base-url is required when ${SERVICE_URL_ENV} is not set")
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
