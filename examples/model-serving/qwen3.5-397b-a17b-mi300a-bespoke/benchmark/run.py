#!/usr/bin/env python3
"""Multi-turn chat benchmark for the from-scratch Qwen3.5 MI300A bundle.

Launches the candidate's ``python3 server.py`` (see ``launcher.py``), plays a
deterministic set of concurrent multi-turn chat sessions against its
OpenAI-compatible ``/v1/chat/completions`` endpoint, and writes the metrics as
a flat JSON object to ``--output-json``. ``p95_ttft_turn2plus_ms`` is the
primary metric; the framework reads it from the top level of that file.

The run is all-or-nothing. If any turn is dropped, errored, empty, or returns
fewer completion tokens than its fixed ``max_tokens`` budget, the benchmark
prints the offending turns, writes no JSON, and exits nonzero, so a faster but
lossy server cannot score.

On success a ``<output-json>.turns.jsonl`` sibling file holds one record per
turn (timestamps, token counts, pacing info) for post-hoc analysis. The
evaluator never reads it.

Session shape (fixed, deterministic, seeded, not a tunable of this script):
48 sessions, 3-6 turns each, user messages are pseudo-paragraphs of varying
length, greedy decoding with ``ignore_eos`` and a per-turn fixed output
budget, 1-8s think-time between a session's turns. ``--concurrency`` caps how
many sessions run at once (default 0, meaning unlimited).

Pacing (``--pacing``, see ``compute_schedule``): ``closed`` sends a session's
next turn as soon as its previous turn's response completes plus think time,
so the offered load tracks the server's own speed. ``scheduled`` (the default)
sends each turn at a precomputed time derived from a fixed reference server
speed, so the offered load does not depend on how fast the server under test
is; a server slower than the reference degrades to closed-loop behavior for
that turn.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import heapq
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import launcher  # noqa: E402

# Workload/pacing-model version. Kept at 4: unlimited concurrency and an
# admission-queue-aware schedule, identical to the multiturn task's v4 workload
# this bundle was ported from.
BENCHMARK_VERSION = 4
METRICS: tuple[str, ...] = (
    "p95_ttft_turn2plus_ms",
    "mean_tpot_ms",
    "total_token_throughput",
    "turn1_ttft_ms",
    "num_completed_turns",
    "num_failed_turns",
    "benchmark_version",
    "pacing_scheduled",
    "ref_ttft_ms",
    "ref_tpot_ms",
    "concurrency",
    "schedule_bound_fraction",
    "offered_turn_rate_per_s",
    "wall_duration_s",
)

SEED = 20260225
N_SESSIONS = 48
MIN_TURNS = 3
MAX_TURNS = 6
MIN_WORDS = 30  # ~40 tokens
MAX_WORDS = 300  # ~400 tokens
MIN_MAX_TOKENS = 80
MAX_MAX_TOKENS = 300
THINK_TIME_MIN_S = 1.0
THINK_TIME_MAX_S = 8.0
# Historical fixed admission-queue cap, kept as a named constant so
# --concurrency 16 (and compute_schedule's default parameter, used by tests
# that predate --concurrency) still reproduce it exactly. The actual cap used
# at run time comes from --concurrency (see resolve_concurrency): 0 (the
# default) means unlimited, i.e. a queue with one slot per session.
CONCURRENCY = 16
SESSION_START_STAGGER_S = 0.4
REQUEST_TIMEOUT_S = 300.0

# Reference server speed the "scheduled" pacing mode assumes (fixed at the
# values the ported v4 workload used, so results stay comparable). The schedule
# is computed once from these constants before the run starts; they do not
# adapt to the server actually under test, which is the point (see
# compute_schedule's docstring). Overridable with --ref-ttft-ms/--ref-tpot-ms
# for exploring how sensitive a result is to the reference assumption.
REF_TTFT_MS = 700.0
REF_TPOT_MS = 110.0

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
    # Populated for the per-turn records file (see ``write_turn_records``);
    # not used by the aggregate computation below.
    send_ts_monotonic: float | None = None
    send_ts_wall: float | None = None
    first_token_ts_wall: float | None = None
    completion_ts_wall: float | None = None
    prompt_tokens: int | None = None
    cached_tokens: int | None = None
    # None for turn 1 (never scheduled, see compute_schedule) and for every
    # turn under --pacing closed (no schedule exists at all). Otherwise True
    # when this turn's send waited for its scheduled time, False when the
    # server was already slower than the reference and the send degraded to
    # closed-loop (fired as soon as the previous turn's response plus think
    # time were ready).
    schedule_bound: bool | None = None
    # This turn's scheduled send time from compute_schedule(), in the same
    # time.perf_counter() basis as send_ts_monotonic. None under the same
    # conditions as schedule_bound (turn 1, or --pacing closed).
    scheduled_send_ts: float | None = None
    # Only populated on turn 1 (turn_index == 1) under --pacing scheduled:
    # the simulated admission-queue wait compute_schedule's discrete-event
    # simulation assigned this session, i.e. T[s][0] minus the raw
    # session_start_delays stagger. None for every other turn, and for every
    # turn under --pacing closed (no schedule exists at all).
    scheduled_admission_delay_s: float | None = None


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


def generate_sessions(seed: int, n_sessions: int = N_SESSIONS) -> list[Session]:
    sessions = []
    for session_id in range(n_sessions):
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
        sessions.append(Session(session_id=session_id, turns=tuple(turns)))
    return sessions


def resolve_concurrency(concurrency: int, n_sessions: int) -> int:
    """Resolve the ``--concurrency`` CLI value to an actual admission-queue size.

    0 (the default) means unlimited: every session may be in flight at once,
    modeled as an admission queue with one slot per session, so
    ``_simulate_admission_starts`` admits every session at its arrival time
    with no queueing and ``compute_schedule`` reduces to pure per-session
    pacing. A positive value is used as-is; 16 reproduces the historical
    fixed cap (see ``CONCURRENCY``).
    """
    return n_sessions if concurrency <= 0 else concurrency


def session_start_delays(n_sessions: int) -> list[float]:
    """The fixed, seed-independent stagger of each session's opening turn.

    Unchanged by --pacing: both modes start session ``i`` at
    ``i * SESSION_START_STAGGER_S`` seconds after the run begins.
    """
    return [index * SESSION_START_STAGGER_S for index in range(n_sessions)]


def _session_reference_duration_s(session: Session, ref_ttft_s: float, ref_tpot_s: float) -> float:
    """Wall time session would occupy an admission slot at the reference speed.

    Sum over the session's turns of the reference round trip (TTFT plus
    per-token decode time for that turn's known output length) plus the
    session's own think times. This is the same per-turn quantity
    ``compute_schedule`` chains turn-to-turn; summed over every turn it is
    the time from the session's admission (turn 1 sent) to its last turn's
    response completing, i.e. how long it holds a concurrency slot.
    """
    return sum(ref_ttft_s + ref_tpot_s * turn.max_tokens for turn in session.turns) + sum(
        turn.think_time_before_s for turn in session.turns
    )


def _simulate_admission_starts(
    sessions: list[Session],
    start_delays: list[float],
    ref_ttft_s: float,
    ref_tpot_s: float,
    concurrency: int,
) -> dict[int, float]:
    """Deterministic discrete-event simulation of the ``concurrency``-slot admission queue.

    Models exactly what the run-time semaphore does (at most ``concurrency``
    sessions active, held for a whole session), but at the fixed reference
    server speed rather than the real server's speed, so the result does not
    depend on which server is under test: sessions arrive at their
    ``start_delays`` and, in arrival order, each is admitted as soon as it
    has arrived and a slot is free; a slot becomes free ``concurrency`` at a
    time, in order of the admitted session's reference completion time
    (arrival plus its ``_session_reference_duration_s``).

    Implemented as the standard "assign to the server that frees up
    earliest" simulation for a c-server FCFS queue: a min-heap holds the
    time each of the ``concurrency`` slots next becomes free (all start at
    0.0, i.e. every slot is free before the run begins). Sessions are
    processed in arrival order; each pops the earliest-free slot, starts at
    ``max(arrival, that slot's free time)``, and pushes back the slot's new
    free time (start plus this session's reference duration). This is
    deterministic (no randomness, and ties broken by arrival order) and
    monotone: because slots are always handed out earliest-free-first to
    sessions processed in nondecreasing arrival order, both admission start
    time and slot free time are nondecreasing in arrival order too.

    Returns a map from session_id to its simulated admission start time, in
    seconds from the run's start.
    """
    order = sorted(range(len(sessions)), key=lambda i: (start_delays[i], i))
    free_at: list[float] = [0.0] * concurrency
    heapq.heapify(free_at)
    starts: dict[int, float] = {}
    for i in order:
        session = sessions[i]
        arrival = start_delays[i]
        earliest_free = heapq.heappop(free_at)
        start = max(arrival, earliest_free)
        starts[session.session_id] = start
        duration = _session_reference_duration_s(session, ref_ttft_s, ref_tpot_s)
        heapq.heappush(free_at, start + duration)
    return starts


def compute_schedule(
    sessions: list[Session],
    start_delays: list[float],
    ref_ttft_ms: float,
    ref_tpot_ms: float,
    concurrency: int = CONCURRENCY,
) -> dict[int, tuple[float, ...]]:
    """Precompute each turn's scheduled send time under --pacing scheduled.

    Purely deterministic given ``sessions`` (already seeded), ``start_delays``,
    and the reference constants: no randomness of its own, so it is
    reproducible and safe to call before the run starts.

    For session ``s`` and turn ``k`` (0-based here; turn 1 in TurnResult's
    1-based numbering is index 0):

        T[s][0] = admission_start[s]
        T[s][k] = T[s][k-1] + ref_ttft_ms + ref_tpot_ms * expected_output_tokens[s][k-1]
                  + think_time[s][k]

    ``admission_start[s]`` comes from ``_simulate_admission_starts``: a
    discrete-event simulation of the ``concurrency``-slot admission queue at
    the reference server speed. Earlier versions of this function set
    ``T[s][0] = start_delays[s]`` directly, ignoring that the run-time
    semaphore only admits ``CONCURRENCY`` sessions at once and holds each for
    a whole session; with 48 sessions and ``CONCURRENCY = 16`` that made
    sessions 16 and later start tens to over a hundred seconds later than
    their turn-1 schedule assumed, so every later turn in those sessions was
    chained from a starting point the real run never used, and their
    schedule_bound_fraction collapsed toward closed-loop pacing regardless of
    server speed. Simulating admission at the reference speed keeps ``T[s][0]``
    consistent with the rest of the schedule: a server at the reference speed
    reproduces the same admission delay the real semaphore would produce.

    ``expected_output_tokens[s][k-1]`` is ``turns[k-1].max_tokens``: decoding
    is greedy with ``ignore_eos``, so the server is instructed to fill the
    turn's output budget exactly and the generation length is known in
    advance rather than sampled from a reference transcript.

    The chaining formula models turn k-1's full round trip at the reference
    server speed (a fixed TTFT plus per-token decode time for its known
    output length) followed by the session's own think time, so T[s][k] is
    the time turn k would be sent if the server always ran at exactly the
    reference speed and admission followed the simulated queue. At run time
    (see run_session) a turn is sent no earlier than its T[s][k], which is
    what makes the offered load stop depending on the real server's speed.

    ``concurrency`` must be the same admission-queue size the caller's real
    ``asyncio.Semaphore`` uses (see ``resolve_concurrency``); it defaults to
    ``CONCURRENCY`` (the historical fixed 16) only so callers that predate
    ``--concurrency`` keep working unchanged. Passing ``concurrency ==
    len(sessions)`` (unlimited, i.e. every session gets its own admission
    slot) makes ``_simulate_admission_starts`` admit every session at its own
    ``start_delays`` entry with no queueing wait at all, so ``T[s][0]``
    reduces to ``start_delays[s]`` and the schedule is pure per-session
    pacing off the reference speed, unaffected by any other session.

    Returns a map from session_id to a tuple of per-turn send times, in
    seconds from the run's start, one entry per turn (index 0 is turn 1).
    Strictly increasing within a session, since ref_ttft_ms > 0 and every
    turn after the first carries a strictly positive think time.
    """
    ref_ttft_s = ref_ttft_ms / 1000.0
    ref_tpot_s = ref_tpot_ms / 1000.0
    admission_starts = _simulate_admission_starts(
        sessions, start_delays, ref_ttft_s, ref_tpot_s, concurrency
    )
    schedule: dict[int, tuple[float, ...]] = {}
    for session in sessions:
        times = [admission_starts[session.session_id]]
        for k in range(1, len(session.turns)):
            prev_turn = session.turns[k - 1]
            think = session.turns[k].think_time_before_s
            times.append(times[-1] + ref_ttft_s + ref_tpot_s * prev_turn.max_tokens + think)
        schedule[session.session_id] = tuple(times)
    return schedule


async def send_chat_turn(
    client,
    base_url: str,
    messages: list[dict],
    max_tokens: int,
) -> TurnAttempt:
    """POST one chat turn and report timing, tokens, and any error."""
    import aiohttp

    body = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
        "ignore_eos": True,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    url = base_url.rstrip("/") + "/v1/chat/completions"
    started = time.perf_counter()
    first_token: float | None = None
    first_token_ts_wall: float | None = None
    parts: list[str] = []
    completion_tokens: int | None = None
    prompt_tokens: int | None = None
    cached_tokens: int | None = None
    try:
        async with client.post(
            url, json=body, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
        ) as resp:
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
                        first_token = time.perf_counter()
                        first_token_ts_wall = time.time()
                    parts.append(content)
    except Exception as exc:  # noqa: BLE001
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


async def run_session(
    client,
    base_url: str,
    session: Session,
    semaphore: asyncio.Semaphore,
    start_delay_s: float,
    schedule: tuple[float, ...] | None = None,
    run_started: float | None = None,
) -> list[TurnResult]:
    """Play one session's turns, closed-loop or against a precomputed schedule.

    ``schedule`` is None under --pacing closed: send logic is then bit-for-bit
    today's closed-loop behavior (sleep think time, send). Otherwise
    ``schedule[k]`` (0-based; index 0 is turn 1) is the turn's scheduled send
    time in seconds from ``run_started``, from compute_schedule(). Turn 1's
    actual send time is unaffected either way: it fires at ``start_delay_s``
    regardless of pacing mode, and real admission into the ``concurrency``-slot
    ``semaphore`` (see ``--concurrency``) still happens here, at run time,
    against the real server.
    ``schedule[0]`` (T[s][0]) is only the *simulated* admission start
    compute_schedule predicted at the reference speed; the gap between the
    two, ``schedule[0] - start_delay_s``, is recorded on turn 1's result as
    ``scheduled_admission_delay_s`` so it can be inspected without re-running
    the simulation.

    For turn k >= 2 under scheduling, the turn is sent at
    ``max(T[s][k], completion_time_of_turn_k_minus_1 + think_time)``: the
    unconditional think-time sleep below already reaches the closed-loop
    ready time, so a further sleep only happens when the schedule is later
    than that, i.e. when the real server is faster than the reference speed
    the schedule assumes. A server slower than the reference never waits past
    its own closed-loop ready time, which is the intended degrade-to-closed-loop
    behavior.
    """
    await asyncio.sleep(start_delay_s)
    admission_delay_s: float | None = None
    if schedule is not None:
        admission_delay_s = schedule[0] - start_delay_s
    results: list[TurnResult] = []
    async with semaphore:
        history: list[dict] = []
        for turn_index, turn in enumerate(session.turns, start=1):
            if turn.think_time_before_s:
                await asyncio.sleep(turn.think_time_before_s)
            schedule_bound: bool | None = None
            scheduled_send_ts: float | None = None
            if schedule is not None and turn_index >= 2:
                assert run_started is not None
                scheduled_send_ts = run_started + schedule[turn_index - 1]
                now = time.perf_counter()
                if scheduled_send_ts > now:
                    await asyncio.sleep(scheduled_send_ts - now)
                    schedule_bound = True
                else:
                    schedule_bound = False
            history.append({"role": "user", "content": turn.user_text})
            send_ts_wall = time.time()
            turn_started = time.perf_counter()
            attempt = await send_chat_turn(client, base_url, history, turn.max_tokens)
            latency_s = time.perf_counter() - turn_started
            completion_ts_wall = time.time()
            error = attempt.error or validate_attempt(attempt, turn.max_tokens)
            ok = error is None
            results.append(
                TurnResult(
                    session_id=session.session_id,
                    turn_index=turn_index,
                    ok=ok,
                    ttft_s=attempt.ttft_s,
                    completion_tokens=attempt.completion_tokens,
                    latency_s=latency_s,
                    error=error,
                    send_ts_monotonic=turn_started,
                    send_ts_wall=send_ts_wall,
                    first_token_ts_wall=attempt.first_token_ts_wall,
                    completion_ts_wall=completion_ts_wall,
                    prompt_tokens=attempt.prompt_tokens,
                    cached_tokens=attempt.cached_tokens,
                    schedule_bound=schedule_bound,
                    scheduled_send_ts=scheduled_send_ts,
                    scheduled_admission_delay_s=(admission_delay_s if turn_index == 1 else None),
                )
            )
            if not ok:
                break  # session's history contract is broken; stop this session.
            history.append({"role": "assistant", "content": attempt.text})
    return results


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    k = (len(ordered) - 1) * p / 100.0
    f, c = int(k), min(int(k) + 1, len(ordered) - 1)
    if f == c:
        return ordered[f]
    return ordered[f] * (c - k) + ordered[c] * (k - f)


async def run_repetition(
    base_url: str,
    pacing: str,
    ref_ttft_ms: float,
    ref_tpot_ms: float,
    concurrency: int,
) -> list[TurnResult]:
    import aiohttp

    sessions = generate_sessions(SEED, N_SESSIONS)
    start_delays = session_start_delays(len(sessions))
    schedule = (
        compute_schedule(sessions, start_delays, ref_ttft_ms, ref_tpot_ms, concurrency=concurrency)
        if pacing == "scheduled"
        else None
    )
    semaphore = asyncio.Semaphore(concurrency)
    run_started = time.perf_counter()
    async with aiohttp.ClientSession() as client:
        tasks = [
            run_session(
                client,
                base_url,
                session,
                semaphore,
                start_delay_s=start_delays[index],
                schedule=schedule[session.session_id] if schedule is not None else None,
                run_started=run_started,
            )
            for index, session in enumerate(sessions)
        ]
        per_session_results = await asyncio.gather(*tasks)
    return [result for results in per_session_results for result in results]


def aggregate_metrics(
    all_results: list[TurnResult],
    wall_s: float,
    *,
    pacing: str,
    ref_ttft_ms: float,
    ref_tpot_ms: float,
    concurrency: int = CONCURRENCY,
) -> dict[str, float]:
    """Reduce one benchmark run's turn results to the reported metric row.

    Separated from run_benchmark so the reduction itself (in particular
    schedule_bound_fraction and offered_turn_rate_per_s) is unit-testable
    without a server.
    """
    ok_results = [r for r in all_results if r.ok]
    failed = [r for r in all_results if not r.ok]
    turn2plus_ttft_ms = [
        r.ttft_s * 1000 for r in ok_results if r.turn_index >= 2 and r.ttft_s is not None
    ]
    turn1_ttft_ms = [
        r.ttft_s * 1000 for r in ok_results if r.turn_index == 1 and r.ttft_s is not None
    ]
    tpot_ms = [
        (r.latency_s - r.ttft_s) / (r.completion_tokens - 1) * 1000
        for r in ok_results
        if r.ttft_s is not None and r.completion_tokens and r.completion_tokens > 1
    ]
    total_completion_tokens = sum(r.completion_tokens or 0 for r in ok_results)

    if not turn2plus_ttft_ms:
        raise RuntimeError(
            f"no successful turn-2+ completions out of {len(all_results)} attempted turns "
            f"({len(failed)} failed); cannot compute p95_ttft_turn2plus_ms. "
            f"First failure: {failed[0].error if failed else '(none)'}"
        )

    turn2plus_sends = [r for r in all_results if r.turn_index >= 2]
    schedule_bound_sends = [r for r in turn2plus_sends if r.schedule_bound]
    schedule_bound_fraction = (
        len(schedule_bound_sends) / len(turn2plus_sends) if turn2plus_sends else 0.0
    )

    return {
        "p95_ttft_turn2plus_ms": percentile(turn2plus_ttft_ms, 95),
        "mean_tpot_ms": (sum(tpot_ms) / len(tpot_ms)) if tpot_ms else float("nan"),
        "total_token_throughput": (total_completion_tokens / wall_s if wall_s > 0 else 0.0),
        "turn1_ttft_ms": (
            (sum(turn1_ttft_ms) / len(turn1_ttft_ms)) if turn1_ttft_ms else float("nan")
        ),
        "num_completed_turns": float(len(ok_results)),
        "num_failed_turns": float(len(failed)),
        "benchmark_version": float(BENCHMARK_VERSION),
        "pacing_scheduled": 1.0 if pacing == "scheduled" else 0.0,
        "ref_ttft_ms": float(ref_ttft_ms),
        "ref_tpot_ms": float(ref_tpot_ms),
        "concurrency": float(concurrency),
        "schedule_bound_fraction": schedule_bound_fraction,
        "offered_turn_rate_per_s": len(all_results) / wall_s if wall_s > 0 else 0.0,
        "wall_duration_s": wall_s,
    }


async def run_benchmark(
    base_url: str,
    repetitions: int,
    *,
    pacing: str,
    ref_ttft_ms: float,
    ref_tpot_ms: float,
    concurrency: int,
) -> tuple[dict[str, float], list[TurnResult]]:
    all_results: list[TurnResult] = []
    wall_started = time.perf_counter()
    for _ in range(repetitions):
        all_results.extend(
            await run_repetition(base_url, pacing, ref_ttft_ms, ref_tpot_ms, concurrency)
        )
    wall_s = time.perf_counter() - wall_started

    values = aggregate_metrics(
        all_results,
        wall_s,
        pacing=pacing,
        ref_ttft_ms=ref_ttft_ms,
        ref_tpot_ms=ref_tpot_ms,
        concurrency=concurrency,
    )
    return values, all_results


def turns_output_path(output_json: Path) -> Path:
    """Sibling path for per-turn records, next to the ``--output-json`` file."""
    return output_json.with_name(output_json.name + ".turns.jsonl")


def write_metrics(path: Path, values: dict[str, float]) -> None:
    """Write the metrics as a flat JSON object (primary metric at top level)."""
    path.write_text(json.dumps(values, indent=2) + "\n")


def check_all_turns_ok(all_results: list[TurnResult], repetitions: int) -> None:
    """Raise unless every planned turn was attempted and succeeded.

    A failed turn ends its session, so the attempted count also catches turns
    that never ran. Reports up to five offenders.
    """
    planned = repetitions * sum(len(s.turns) for s in generate_sessions(SEED, N_SESSIONS))
    bad = [r for r in all_results if not r.ok]
    if not bad and len(all_results) == planned:
        return
    lines = [f"session {r.session_id} turn {r.turn_index}: {r.error}" for r in bad[:5]]
    raise RuntimeError(
        f"{len(bad)} failed turn(s) and {planned - len(all_results)} unattempted "
        f"turn(s) of {planned} planned; the benchmark requires zero. " + "; ".join(lines)
    )


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
                "schedule_bound": result.schedule_bound,
                "scheduled_send_ts": result.scheduled_send_ts,
                "scheduled_admission_delay_s": result.scheduled_admission_delay_s,
                "error": result.error,
            }
            handle.write(json.dumps(record) + "\n")


async def main_async(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    output_path = Path(args.output_json)
    log_path = workspace / ".vibesys-benchmark-server.log"

    try:
        model_path = args.model_path or launcher.resolve_model_path(required=args.base_url is None)
        async with launcher.server_endpoint(
            base_url=args.base_url,
            workspace=workspace,
            model_path=model_path,
            host=args.host,
            port=args.port,
            log_path=log_path,
            startup_timeout_seconds=args.startup_timeout_seconds,
        ) as base_url:
            concurrency = resolve_concurrency(args.concurrency, N_SESSIONS)
            values, all_results = await run_benchmark(
                base_url,
                args.repetitions,
                pacing=args.pacing,
                ref_ttft_ms=args.ref_ttft_ms,
                ref_tpot_ms=args.ref_tpot_ms,
                concurrency=concurrency,
            )
        check_all_turns_ok(all_results, args.repetitions)
    except Exception as exc:  # noqa: BLE001
        print(f"benchmark failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        output_path.unlink(missing_ok=True)
        return 1

    print(
        f"pacing={args.pacing} "
        "p95_ttft_turn2plus_ms="
        f"{values['p95_ttft_turn2plus_ms']:.1f} mean_tpot_ms={values['mean_tpot_ms']:.1f} "
        f"total_token_throughput={values['total_token_throughput']:.1f} "
        f"turn1_ttft_ms={values['turn1_ttft_ms']:.1f} "
        f"num_completed_turns={values['num_completed_turns']:.0f} "
        f"concurrency={values['concurrency']:.0f} "
        f"schedule_bound_fraction={values['schedule_bound_fraction']:.3f} "
        f"offered_turn_rate_per_s={values['offered_turn_rate_per_s']:.3f} "
        f"wall_duration_s={values['wall_duration_s']:.1f}"
    )
    write_metrics(output_path, values)
    write_turn_records(turns_output_path(output_path), all_results)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        default=".",
        help="Workspace root containing server.py (default: current directory).",
    )
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument(
        "--model-path", default=None, help="Model directory. Defaults to $MODEL_PATH."
    )
    parser.add_argument("--host", default=launcher.DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=launcher.DEFAULT_PORT)
    parser.add_argument(
        "--startup-timeout-seconds",
        type=float,
        default=launcher.STARTUP_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Run against an already-running server at this URL instead of booting one.",
    )
    parser.add_argument(
        "--output-json",
        required=True,
        help="Path the metrics JSON is written to (only on a fully clean run).",
    )
    parser.add_argument(
        "--pacing",
        choices=("closed", "scheduled"),
        default="scheduled",
        help=(
            "closed: send a session's next turn as soon as the previous turn's "
            "response completes plus think time, so offered load tracks the "
            "server's own speed. scheduled (default): send turns at a "
            "precomputed schedule keyed to a fixed reference server speed, so "
            "offered load stops depending on the server under test. See "
            "README.md."
        ),
    )
    parser.add_argument(
        "--ref-ttft-ms",
        type=float,
        default=REF_TTFT_MS,
        help="Reference TTFT the --pacing scheduled schedule assumes.",
    )
    parser.add_argument(
        "--ref-tpot-ms",
        type=float,
        default=REF_TPOT_MS,
        help="Reference TPOT the --pacing scheduled schedule assumes.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=0,
        help=(
            "Max sessions in flight at once (an asyncio.Semaphore of this "
            "size, and the same value the --pacing scheduled admission-queue "
            "simulation uses). 0 (the default) means unlimited: all 48 "
            "sessions may run at once. Pass 16 to reproduce the historical "
            "fixed cap (benchmark_version 3 and earlier)."
        ),
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
