"""Concurrency sweep over real TraceLab traffic with trusted Request Factory.

The sweep replays a 2,000-session TraceLab trace at rising session concurrency, one
fixed-time window per step. It climbs while a step meets the latency cap and keeps gaining
throughput, always measures the fixed level 4, and may add one refinement step. The score is
three VibeSys objectives: the peak throughput over the steps that meet the cap, and TTFT p50 /
TPOT p50 at the fixed level 4.

Public API (pure, unit-tested in ``test_replay.py``): ``step_metrics`` validates one
window's summary and extracts its metrics, ``passes_cap`` / ``climb_stopped`` /
``plan_next`` plan the steps, ``refinement_concurrency`` and ``choose_peak`` pick the
candidates and the peak, ``next_step_fits`` enforces the overall time budget and
``build_result`` assembles the protocol-2 values.

``--sweep`` (a development run, e.g. ``vibesys-cli submit command <sha> --image harness --``)
runs a small set of concurrency levels chosen by arguments after ``--``, or the defaults: the
same per-step machinery with shorter windows, a table instead of a result, never scored.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

MODEL = "Qwen/Qwen3.6-35B-A3B"
REVISION = "995ad96eacd98c81ed38be0c5b274b04031597b0"
TRACE = Path(__file__).with_name("trace.csv")
TRACE_SHA256 = "a2546a138e9fcb48e583faeb696b2fe1e748b6a5524b7c1bcae1183f859d66bc"

# ---- Sweep parameters. These are the task's fixed policy; only the hidden --test-* flags
# (which the benchmark command never passes, and which need an environment opt-in) change them.
CONCURRENCIES = (4, 8, 16, 32, 64, 128, 256)
# Scored fixed-load level: always measured, whatever the climb does; the climb starts there.
FIXED_LEVELS = (4,)
# The timing of every step: one place to change it later.
WARMUP_SECS = 60
DURATION_SECS = 180  # the measured window
COOLDOWN_SECS = 15  # lets the server drain the work cancelled at window end
# A step passes the latency cap if its TPOT p50 and TTFT p50 are within these.
TPOT_P50_CAP_MS = 100.0
TTFT_P50_CAP_MS = 10_000.0
# A step must beat the best earlier passing throughput by this factor to keep the climb going.
SATURATION_GAIN = 1.03
# The reported peak concurrency is the lowest passing step within this fraction of the maximum.
PEAK_TOLERANCE = 0.03
REFINE_FACTOR = math.sqrt(2)
MAX_MODEL_LEN = 262144

# Sweep mode (unscored, development): defaults and limits of the settings after `--`.
SWEEP_CONCURRENCY = (4, 16, 64)
SWEEP_WARMUP_SECS = 30
SWEEP_DURATION_SECS = 60
SWEEP_COOLDOWN_SECS = 10
SWEEP_MAX_STEPS = 8
SWEEP_MAX_CONCURRENCY = 512
SWEEP_MAX_TOTAL_SECS = 1500  # keeps a development run well inside half an hour
SWEEP_KEYS = ("concurrency", "warmup_s", "window_s", "cooldown_s")
SWEEP_NOTICE = (
    "These sweep numbers are UNSCORED and not comparable with `submit measure` numbers: "
    "the windows are shorter and sit earlier in the server's session."
)

# ---- Time budget, inside the manifest's 3600 s benchmark timeout.
TOTAL_TIMEOUT_SECS = 3600
SAFETY_MARGIN_SECS = 300  # interpreter start, cleanup, and slack against the hard timeout
DEADLINE_SECS = TOTAL_TIMEOUT_SECS - SAFETY_MARGIN_SECS
# The runner's own setup outside the window: tokenizing the pool and the prefix-cache preflight.
SETUP_ESTIMATE_SECS = 90  # planning figure for a step before any step has been observed
SETUP_TIMEOUT_SECS = 300  # hard allowance on top of warmup + duration for one runner process

# ---- Synthetic token pool.
# TraceLab supplies token counts and topology, not prompt text, so the runner fills prompts
# from a pool of token ids it tokenizes out of --text-file. It walks the pool from a
# per-session offset (session ordinal * 9,973) and wraps at the pool's end. Two things follow.
#  1. A prompt of up to ~262k tokens (the trace's peak context is 260,791) repeats synthetic
#     content only if the pool is shorter than that, so the pool must exceed it by a wide
#     margin. 8,000,000 tokens is about 30x the longest prompt.
#  2. Two sessions share a start offset only if 9,973 * (ordinal difference) is a multiple of
#     the pool length. 9,973 is prime, so with a pool length that is not a multiple of it and
#     larger than the 2,000 session ordinals, no two sessions ever share a start offset, and
#     sessions cannot fabricate cross-session prefix-cache hits.
# --token-pool-limit is passed explicitly (the runner's own default would be 100M tokens,
# ~400 MB and tens of seconds of tokenizing per step). Each corpus line carries its index and
# a 128-bit nonce, which tokenize to well over 20 tokens, so CORPUS_LINES lines yield at least
# 10M tokens, more than the limit; the runner stops tokenizing at the limit.
TOKEN_POOL_LIMIT = 8_000_000
CORPUS_LINES = 500_000

METRICS = {
    "output_tokens_per_s": "output_token_throughput_per_s",
    "ttft_p50_ms": "ttft_ms_p50",
    "ttft_p90_ms": "ttft_ms_p90",
    "tpot_p50_ms": "tpot_ms_p50",
    "tpot_p90_ms": "tpot_ms_p90",
}


def refinement_candidates() -> tuple[int, ...]:
    """Every concurrency a refinement step can have: round(p * sqrt(2)) for p below the top."""
    return tuple(_refine(p) for p in CONCURRENCIES[:-1])


def _refine(p: int) -> int:
    return math.floor(p * REFINE_FACTOR + 0.5)


# The three objectives are required; everything else is reported and may be absent.
OBJECTIVES = ["output_tokens_per_s"] + [
    f"{m}_c{c}" for c in FIXED_LEVELS for m in ("ttft_p50_ms", "tpot_p50_ms")
]
HELLO = {
    "kind": "hello",
    "protocol": 2,
    "metrics": {
        "output_tokens_per_s": {"unit": "tokens/s", "direction": "max"},
        **{
            f"{m}_c{c}": {"unit": "ms", "direction": "min"}
            for c in FIXED_LEVELS
            for m in ("ttft_p50_ms", "tpot_p50_ms")
        },
        "peak_concurrency": {"unit": "sessions", "required": False},
        "latency_cap_met": {"unit": "flag", "required": False},
        **{
            f"tok_s_at_c{c}": {"unit": "tokens/s", "direction": "max", "required": False}
            for c in sorted(set(CONCURRENCIES) | set(refinement_candidates()))
        },
        **{
            f"{m}_c{c}": {"unit": "ms", "direction": "min", "required": False}
            for c in FIXED_LEVELS
            for m in ("ttft_p90_ms", "tpot_p90_ms")
        },
    },
}


def write_record(path: Path, record: dict[str, Any], *, first: bool = False) -> None:
    """Write one protocol record line: truncate the stream if first, else append."""
    with path.open("w" if first else "a") as stream:
        stream.write(json.dumps(record, allow_nan=False) + "\n")


def _finite_nonnegative(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(value)
        and value >= 0
    )


def step_metrics(summary: object, warmup_secs: float, duration_secs: float) -> dict[str, float]:
    """Validate one window's summary and return its five objective metrics.

    Any failed request, wrong output length, request whose tokens could not be placed in
    time, empty window, or missing/non-finite metric raises ValueError, which fails the whole
    measurement. The one exception is a window into which no request was submitted: its
    latencies are undefined, and are censored at the window's length (overload).
    """
    if not isinstance(summary, dict) or not isinstance(summary.get("replay"), dict):
        raise ValueError("summary.replay must be an object")
    replay = summary["replay"]
    if replay.get("kind") != "sessions" or not isinstance(replay.get("common"), dict):
        raise ValueError("summary.replay must describe sessions with common metrics")
    common = replay["common"]
    window = replay.get("window")
    if not isinstance(window, dict):
        raise ValueError("summary.replay.window is missing: the runner did not window the replay")
    for key, expected in {"warmup_s": warmup_secs, "duration_s": duration_secs}.items():
        if not _finite_nonnegative(window.get(key)) or window[key] != expected:
            raise ValueError(f"summary.replay.window.{key} must equal {expected}")

    def count(container: dict[str, Any], where: str, key: str) -> int:
        value = container.get(key)
        if type(value) is not int or value < 0:
            raise ValueError(f"summary.replay.{where}.{key} must be a nonnegative integer")
        return value

    for where, container, key, what in (
        ("common", common, "failed_steps", "failed requests"),
        ("window", window, "requests_failed", "failed requests in the window"),
        ("window", window, "warmup_requests_failed", "failed requests during warmup"),
        (
            "window",
            window,
            "requests_output_length_mismatch",
            "requests with the wrong output length",
        ),
        (
            "window",
            window,
            "requests_without_token_ids",
            "requests whose server streamed no token ids (the server must return them, "
            "or throughput is undercounted)",
        ),
    ):
        found = count(container, where, key)
        if found:
            raise ValueError(f"{found} {what} (summary.replay.{where}.{key})")
    if count(window, "window", "output_tokens") <= 0:
        raise ValueError("the window produced no output tokens")
    values = {}
    # A window into which no request was submitted (every session was still busy with a
    # request sent earlier) measures no latency: the runner reports it as null. That is
    # overload, not a malformed run, so the latency is censored at the window's length,
    # which fails the latency cap; any other missing or non-finite metric is an error.
    idle_window = count(window, "window", "requests_submitted") == 0
    for name, field in METRICS.items():
        value = common.get(field)
        if value is None and idle_window and name != "output_tokens_per_s":
            value = duration_secs * 1000.0
        if not _finite_nonnegative(value):
            raise ValueError(f"summary.replay.common.{field} must be finite and nonnegative")
        values[name] = float(value)
    if values["output_tokens_per_s"] <= 0:
        raise ValueError("output throughput must be positive")
    return values


def window_counts(summary: dict[str, Any]) -> dict[str, Any]:
    """Diagnostics of one validated window (sweep mode reports them)."""
    replay = summary["replay"]
    window = replay["window"]
    rate = (replay.get("prefix_cache") or {}).get("server_prefix_hit_rate")
    return {
        "requests_submitted": window.get("requests_submitted"),
        "ttft_censored_requests": window.get("ttft_censored_requests"),
        "requests_cancelled_at_window_end": window.get("requests_cancelled_at_window_end"),
        "server_prefix_hit_rate": float(rate) if _finite_nonnegative(rate) else None,
    }


Row = dict[str, float]


def passes_cap(row: Row) -> bool:
    """Whether a step meets the latency cap: TPOT p50 and TTFT p50 within their limits."""
    return row["tpot_p50_ms"] <= TPOT_P50_CAP_MS and row["ttft_p50_ms"] <= TTFT_P50_CAP_MS


def climb_stopped(rows: list[Row]) -> bool:
    """Whether the climb, given its steps in order, has ended.

    The climb continues while each step passes the cap and its throughput is at least
    SATURATION_GAIN times the best throughput of the earlier passing steps. The first step
    that fails either ends it.
    """
    best = 0.0
    for row in rows:
        tput = row["output_tokens_per_s"]
        if not passes_cap(row) or tput < SATURATION_GAIN * best:
            return True
        best = max(best, tput)
    return False


def best_passing(history: dict[int, Row]) -> int | None:
    """The passing concurrency with the highest throughput; a tie goes to the lower one."""
    passing = [c for c, row in history.items() if passes_cap(row)]
    if not passing:
        return None
    return max(passing, key=lambda c: (history[c]["output_tokens_per_s"], -c))


def refinement_concurrency(history: dict[int, Row]) -> int | None:
    """The one refinement step: round(p * sqrt(2)) when both p (best passing) and 2p ran."""
    p = best_passing(history)
    if p is None or 2 * p not in history:
        return None
    candidate = _refine(p)
    return None if candidate in history else candidate


def plan_next(
    levels: tuple[int, ...], history: dict[int, Row], skipped: frozenset[int] = frozenset()
) -> int | None:
    """The next concurrency to measure, or None when the sweep is complete.

    Rule: levels up to max(FIXED_LEVELS) (4) always run, in order. Above that the climb
    goes on only while it has not stopped (see ``climb_stopped``). When the climb ends, one
    refinement step may follow. ``history`` maps measured concurrency to its row; ``skipped``
    holds levels the time budget dropped.
    """
    fixed_top = max(FIXED_LEVELS)
    climbed = [row for c, row in history.items() if c in levels]
    stopped = climb_stopped(climbed)
    for c in levels:
        if c in history or c in skipped:
            continue
        if c <= fixed_top or not stopped:
            return c
        break
    return refinement_concurrency(history)


def choose_peak(history: dict[int, Row]) -> tuple[float, int, bool]:
    """(throughput, concurrency, latency cap met) of the scored peak.

    The throughput is the maximum over the passing steps; the concurrency is the lowest passing
    step within PEAK_TOLERANCE of it. With no passing step, the lowest step measured stands in
    and the cap is reported unmet.
    """
    if not history:
        raise ValueError("no step ran")
    passing = {c: row["output_tokens_per_s"] for c, row in history.items() if passes_cap(row)}
    if not passing:
        lowest = min(history)
        return history[lowest]["output_tokens_per_s"], lowest, False
    top = max(passing.values())
    peak = min(c for c, tput in passing.items() if tput >= (1 - PEAK_TOLERANCE) * top)
    return top, peak, True


def next_step_fits(
    elapsed: float,
    *,
    warmup: float,
    duration: float,
    cooldown: float,
    setup_estimate: float,
    deadline: float,
    steps: int = 1,
) -> bool:
    """Whether `steps` further steps (each: cooldown, runner setup, window) end before the deadline."""
    return elapsed + steps * (cooldown + setup_estimate + warmup + duration) <= deadline


def build_result(history: dict[int, Row]) -> dict[str, float]:
    """Protocol-2 values: the three objectives plus the reported diagnostics."""
    missing = [c for c in FIXED_LEVELS if c not in history]
    if missing:
        raise ValueError(f"fixed level(s) {missing} were not measured")
    top, peak, cap_met = choose_peak(history)
    values: dict[str, float] = {"output_tokens_per_s": top}
    for c in FIXED_LEVELS:
        for name in ("ttft_p50_ms", "tpot_p50_ms", "ttft_p90_ms", "tpot_p90_ms"):
            values[f"{name}_c{c}"] = history[c][name]
    values["peak_concurrency"] = float(peak)
    values["latency_cap_met"] = 1.0 if cap_met else 0.0
    for c, row in history.items():
        values[f"tok_s_at_c{c}"] = row["output_tokens_per_s"]
    return values


def format_table(history: dict[int, Row]) -> str:
    """The per-step table written to stderr."""
    _, peak, cap_met = choose_peak(history)
    lines = ["conc    tok/s  ttft_p50  ttft_p90  tpot_p50  tpot_p90  cap"]
    for c, row in history.items():
        lines.append(
            f"{c:4d} {row['output_tokens_per_s']:8.1f} {row['ttft_p50_ms']:9.0f} "
            f"{row['ttft_p90_ms']:9.0f} {row['tpot_p50_ms']:9.1f} {row['tpot_p90_ms']:9.1f}  "
            + ("ok " if passes_cap(row) else "FAIL")
            + ("  <- peak" if c == peak and cap_met else "")
        )
    if not cap_met:
        lines.append("no step met the latency cap: the lowest step stands in for the peak")
    return "\n".join(lines)


def _tokenizer() -> Path:
    """The exact-revision tokenizer.json from the operator's read-only Hugging Face cache.

    The task image runs offline: the cache is the mount HF_HOME names, laid out as
    huggingface_hub lays it out (hub/models--<org>--<name>/snapshots/<revision>/).
    """
    home = os.environ.get("HF_HOME")
    if not home:
        raise ValueError("HF_HOME is not set: the task needs the Hugging Face cache mount")
    path = (
        Path(home)
        / "hub"
        / f"models--{MODEL.replace('/', '--')}"
        / "snapshots"
        / REVISION
        / "tokenizer.json"
    )
    if not path.is_file():
        raise ValueError(f"{path} is missing from the Hugging Face cache")
    return path


def write_corpus(path: Path, lines: int) -> None:
    """A synthetic coding-text corpus whose every line is unique and carries a fresh nonce.

    A new nonce per call keeps one step's text from matching the server's prefix cache with
    another step's: all steps replay the same session prefixes. Every line holds the nonce, so
    no line (and no short run of tokens) repeats between steps.
    """
    nonce = secrets.token_hex(16)
    with path.open("w") as stream:
        for index in range(lines):
            stream.write(
                f"def case_{index}_{nonce}(items): return sorted(set(items))  # {index} {nonce}\n"
            )


def run_step(
    args: argparse.Namespace,
    plan: argparse.Namespace,
    temporary: Path,
    concurrency: int,
    tokenizer: Path,
    timeout: float,
) -> tuple[Row, dict[str, Any]]:
    """One session_runner invocation: a fresh corpus, one fixed-time window.

    Returns the validated metrics and the window's diagnostics.
    """
    corpus = temporary / f"corpus_c{concurrency}.txt"
    summary = temporary / f"summary_c{concurrency}.json"
    write_corpus(corpus, plan.corpus_lines)
    command = [
        args.request_factory_engine,
        "--trace",
        str(TRACE),
        "--input-file-format",
        "text-generation-session-execution-v2",
        "--text-file",
        str(corpus),
        "--tokenizer",
        str(tokenizer),
        "--token-pool-limit",
        str(plan.pool_limit),
        "--base-url",
        args.url.rstrip("/").removesuffix("/v1") + "/v1",
        "--model",
        plan.model,
        "--backend",
        "openai",
        "--temperature",
        "0",
        "--arrival-mode",
        "saturated",
        "--max-concurrency",
        str(concurrency),
        "--warmup-secs",
        str(plan.warmup),
        "--duration-secs",
        str(plan.duration),
        "--max-model-len",
        str(MAX_MODEL_LEN),
        "--request-log",
        "false",
        "--timeline",
        "false",
        "--summary-path",
        str(summary),
    ]
    try:
        # lint-waiver: LW-994901 [S603]; the evaluator injects the trusted binary;
        # argv execution preserves its boundary, whereas a shell adds interpretation.
        subprocess.run(command, check=True, timeout=timeout)  # noqa: S603
        data = json.loads(summary.read_text())
        return step_metrics(data, plan.warmup, plan.duration), window_counts(data)
    finally:
        corpus.unlink(missing_ok=True)
        summary.unlink(missing_ok=True)


def _check_trace() -> None:
    if hashlib.sha256(TRACE.read_bytes()).hexdigest() != TRACE_SHA256:
        raise ValueError("trace.csv checksum mismatch")


def _step_line(concurrency: int, row: Row, elapsed: float) -> str:
    return (
        f"step c={concurrency}: {row['output_tokens_per_s']:.1f} tok/s, "
        f"ttft p50/p90 {row['ttft_p50_ms']:.0f}/{row['ttft_p90_ms']:.0f} ms, "
        f"tpot p50/p90 {row['tpot_p50_ms']:.1f}/{row['tpot_p90_ms']:.1f} ms, "
        f"cap {'met' if passes_cap(row) else 'NOT met'}, {elapsed:.0f} s elapsed\n"
    )


def sweep(args: argparse.Namespace, plan: argparse.Namespace, temporary: Path) -> dict[str, float]:
    """Run the planned steps and return the protocol-2 values."""
    _check_trace()
    missing = [c for c in FIXED_LEVELS if c not in plan.concurrencies]
    if missing:
        raise ValueError(f"the fixed levels {missing} must be among the steps")
    tokenizer = Path(plan.tokenizer) if plan.tokenizer else _tokenizer()
    started = time.monotonic()
    window = plan.warmup + plan.duration
    history: dict[int, Row] = {}
    skipped: set[int] = set()
    setup_estimate = plan.setup_estimate
    fixed_top = max(FIXED_LEVELS)
    while (concurrency := plan_next(plan.concurrencies, history, frozenset(skipped))) is not None:
        elapsed = time.monotonic() - started
        # Fixed levels have priority: the step must leave room for the fixed levels still to
        # run after it. A fixed level that cannot fit is an error; a lower unfixed level is
        # dropped instead; steps above them and the refinement just end the sweep.
        later_fixed = [c for c in FIXED_LEVELS if c not in history and c != concurrency]
        steps = 1 + (len(later_fixed) if concurrency <= fixed_top else 0)
        if not next_step_fits(
            elapsed,
            warmup=plan.warmup,
            duration=plan.duration,
            cooldown=plan.cooldown,
            setup_estimate=setup_estimate,
            deadline=plan.deadline,
            steps=steps,
        ):
            if concurrency in FIXED_LEVELS:
                raise ValueError(
                    f"the fixed levels {list(FIXED_LEVELS)} cannot finish by the "
                    f"{plan.deadline:.0f} s deadline ({elapsed:.0f} s used before c={concurrency})"
                )
            if concurrency <= fixed_top:
                sys.stderr.write(
                    f"skipping concurrency {concurrency} to leave time for the fixed levels\n"
                )
                skipped.add(concurrency)
                continue
            sys.stderr.write(
                f"stopping before concurrency {concurrency}: {elapsed:.0f} s used, the next "
                f"step would not end by the {plan.deadline:.0f} s deadline\n"
            )
            break
        if history:
            time.sleep(plan.cooldown)
            elapsed = time.monotonic() - started
        timeout = min(window + plan.setup_timeout, plan.deadline + SAFETY_MARGIN_SECS - elapsed)
        step_started = time.monotonic()
        row, _ = run_step(args, plan, temporary, concurrency, tokenizer, timeout)
        setup_estimate = max(
            setup_estimate if history else 0.0, time.monotonic() - step_started - window
        )
        history[concurrency] = row
        sys.stderr.write(_step_line(concurrency, row, time.monotonic() - started))
    sys.stderr.write(format_table(history) + "\n")
    return build_result(history)


class SweepArgsError(ValueError):
    """Invalid sweep settings (from the arguments after `--`)."""


def _sweep_int(text: object, name: str, label: str) -> int:
    if type(text) is int:
        return text
    if not isinstance(text, str) or not re.fullmatch(r"[0-9]+", text):
        raise SweepArgsError(f"{label}: {name} must be a non-negative integer, got {text!r}")
    return int(text)


def validate_sweep(raw: dict[str, Any], label: str) -> dict[str, Any]:
    """The one validation of sweep settings, shared by the arguments and the defaults.

    `raw` maps any of SWEEP_KEYS to a value (ints, or digit strings from the command line);
    missing keys take the defaults. Returns the canonical settings dict.
    """
    unknown = sorted(set(raw) - set(SWEEP_KEYS))
    if unknown:
        raise SweepArgsError(f"{label}: unknown key(s) {unknown}; the keys are {list(SWEEP_KEYS)}")
    levels = raw.get("concurrency", list(SWEEP_CONCURRENCY))
    if (
        not isinstance(levels, list)
        or not 1 <= len(levels) <= SWEEP_MAX_STEPS
        or any(type(c) is not int or not 1 <= c <= SWEEP_MAX_CONCURRENCY for c in levels)
    ):
        raise SweepArgsError(
            f"{label}: concurrency must be a list of 1 to {SWEEP_MAX_STEPS} integers, "
            f"each from 1 to {SWEEP_MAX_CONCURRENCY}"
        )
    if len(set(levels)) != len(levels):
        raise SweepArgsError(f"{label}: concurrency levels must be unique, got {levels}")
    config: dict[str, Any] = {"concurrency": list(levels)}
    for key, default, positive in (
        ("warmup_s", SWEEP_WARMUP_SECS, False),
        ("window_s", SWEEP_DURATION_SECS, True),
        ("cooldown_s", SWEEP_COOLDOWN_SECS, False),
    ):
        value = raw.get(key, default)
        if type(value) is not int or value < 0 or (positive and value == 0):
            raise SweepArgsError(
                f"{label}: {key} must be an integer " + (">= 1" if positive else ">= 0")
            )
        config[key] = value
    planned = sweep_total_secs(config)
    if planned > SWEEP_MAX_TOTAL_SECS:
        raise SweepArgsError(
            f"{label}: the plan needs about {planned:.0f} s, above the "
            f"{SWEEP_MAX_TOTAL_SECS} s cap ({len(levels)} steps x (warmup {config['warmup_s']} "
            f"+ window {config['window_s']} + cooldown {config['cooldown_s']} + "
            f"{SETUP_ESTIMATE_SECS} s setup allowance)); use fewer concurrency levels or shorter "
            "warmup / window / cooldown"
        )
    return config


def canonical_sweep_args(config: dict[str, Any]) -> list[str]:
    """The canonical argument form of validated settings (fixed key order, defaults filled)."""
    return [
        "--concurrency", ",".join(str(c) for c in config["concurrency"]),
        "--warmup-s", str(config["warmup_s"]),
        "--window-s", str(config["window_s"]),
        "--cooldown-s", str(config["cooldown_s"]),
    ]  # fmt: skip


class _SweepParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise SweepArgsError(f"sweep args: {message}")


class _Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):  # type: ignore[no-untyped-def]
        if getattr(namespace, self.dest) is not None:
            raise SweepArgsError(f"sweep args: {option_string} given more than once")
        setattr(namespace, self.dest, values)


def parse_sweep_args(tokens: list[str]) -> dict[str, Any]:
    """Parse the arguments after `--` with a parser that knows only the sweep settings."""
    parser = _SweepParser(prog="-- (sweep args)", add_help=False, allow_abbrev=False)
    for flag, dest in (
        ("--concurrency", "concurrency"),
        ("--warmup-s", "warmup_s"),
        ("--window-s", "window_s"),
        ("--cooldown-s", "cooldown_s"),
    ):
        parser.add_argument(flag, dest=dest, action=_Once, default=None)
    namespace = parser.parse_args(tokens)
    raw: dict[str, Any] = {}
    if namespace.concurrency is not None:
        raw["concurrency"] = [
            _sweep_int(part, "concurrency level", "sweep args")
            for part in namespace.concurrency.split(",")
        ]
    for key in ("warmup_s", "window_s", "cooldown_s"):
        value = getattr(namespace, key)
        if value is not None:
            raw[key] = _sweep_int(value, "--" + key.replace("_", "-"), "sweep args")
    return raw


def resolve_sweep(tokens: list[str] | None) -> dict[str, Any]:
    """Validate the sweep settings: the arguments after `--`, with the defaults filling the rest."""
    return validate_sweep(parse_sweep_args(tokens or []), "sweep args")


def sweep_total_secs(config: dict[str, Any]) -> float:
    """Planned time of a sweep run: per step warmup + window + cooldown + setup allowance."""
    per_step = config["warmup_s"] + config["window_s"] + config["cooldown_s"] + SETUP_ESTIMATE_SECS
    return len(config["concurrency"]) * per_step


def format_sweep_table(steps: list[dict[str, Any]]) -> str:
    """The per-step table printed to stdout."""
    lines = ["conc    tok/s  ttft_p50  ttft_p90  tpot_p50  tpot_p90  requests  ttft_censored  cap"]
    for step in steps:
        if step["error"] is not None:
            lines.append(f"{step['concurrency']:4d} FAILED: {step['error']}")
            continue
        requests = step["requests"]
        lines.append(
            f"{step['concurrency']:4d} {step['output_tokens_per_s']:8.1f} "
            f"{step['ttft_p50_ms']:9.0f} {step['ttft_p90_ms']:9.0f} "
            f"{step['tpot_p50_ms']:9.1f} {step['tpot_p90_ms']:9.1f} "
            f"{'n/a' if requests is None else requests:>9} "
            f"{'n/a' if step['ttft_censored_requests'] is None else step['ttft_censored_requests']:>14}  "
            + ("ok" if step["latency_cap_met"] else "FAIL")
        )
    return "\n".join(lines)


def dev_sweep(
    args: argparse.Namespace, plan: argparse.Namespace, config: dict[str, Any], temporary: Path
) -> dict[str, Any]:
    """Run the configured steps in order, unscored; a failed step is reported and the rest run."""
    _check_trace()
    tokenizer = Path(plan.tokenizer) if plan.tokenizer else _tokenizer()
    plan.warmup, plan.duration = config["warmup_s"], config["window_s"]
    timeout = plan.warmup + plan.duration + plan.setup_timeout
    steps: list[dict[str, Any]] = []
    for index, concurrency in enumerate(config["concurrency"]):
        if index:
            time.sleep(config["cooldown_s"])
        entry: dict[str, Any] = {"concurrency": concurrency, "error": None}
        try:
            row, counts = run_step(args, plan, temporary, concurrency, tokenizer, timeout)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            entry["error"] = str(exc) or type(exc).__name__
            sys.stderr.write(f"sweep step c={concurrency} failed: {entry['error']}\n")
        else:
            entry.update(row)
            entry.update(counts)
            entry["requests"] = counts["requests_submitted"]
            entry["latency_cap_met"] = passes_cap(row)
        steps.append(entry)
    for entry in steps:
        entry.setdefault("requests", None)
        entry.setdefault("ttft_censored_requests", None)
    return {
        "mode": "sweep",
        "args": canonical_sweep_args(config),
        "unscored": True,
        "notice": SWEEP_NOTICE,
        "warmup_s": config["warmup_s"],
        "window_s": config["window_s"],
        "cooldown_s": config["cooldown_s"],
        "steps": steps,
    }


def report_sweep(report: dict[str, Any]) -> None:
    """Print the sweep report and, if VIBESYS_ARTIFACTS_DIR is set, keep it as sweep.json."""
    sys.stdout.write(
        f"TraceLab sweep (warmup {report['warmup_s']:g} s, window {report['window_s']:g} s, "
        f"cooldown {report['cooldown_s']:g} s)\n{format_sweep_table(report['steps'])}\n"
        f"{SWEEP_NOTICE}\n"
    )
    sys.stdout.flush()
    directory = os.environ.get("VIBESYS_ARTIFACTS_DIR")
    if directory:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        (target / "sweep.json").write_text(json.dumps(report, allow_nan=False, indent=2) + "\n")


def _test_plan(args: argparse.Namespace) -> argparse.Namespace:
    """The sweep plan: the task's fixed policy, unless a --test-* flag overrides it.

    The overrides exist to smoke-test the adapter against a fake server. The benchmark command
    passes none of them, and they are refused unless TRACELAB_SCALE_TESTING=1 is also set.
    """
    overrides = {
        "concurrencies": args.test_concurrencies,
        "warmup": args.test_warmup_secs,
        "duration": args.test_duration_secs,
        "cooldown": args.test_cooldown_secs,
        "deadline": args.test_deadline_secs,
        "pool_limit": args.test_pool_limit,
        "corpus_lines": args.test_corpus_lines,
        "tokenizer": args.test_tokenizer,
        "model": args.test_model,
    }
    used = [name for name, value in overrides.items() if value is not None]
    if used and os.environ.get("TRACELAB_SCALE_TESTING") != "1":
        raise ValueError(
            "--test-* flags need TRACELAB_SCALE_TESTING=1 and are for smoke tests only"
        )
    concurrencies = (
        tuple(int(c) for c in (args.test_concurrencies or "").split(",") if c) or CONCURRENCIES
    )
    if list(concurrencies) != sorted(set(concurrencies)) or not set(concurrencies) <= set(
        CONCURRENCIES
    ):
        raise ValueError(f"--test-concurrencies must be an ascending subset of {CONCURRENCIES}")
    return argparse.Namespace(
        concurrencies=concurrencies,
        warmup=args.test_warmup_secs if args.test_warmup_secs is not None else WARMUP_SECS,
        duration=args.test_duration_secs if args.test_duration_secs is not None else DURATION_SECS,
        cooldown=args.test_cooldown_secs if args.test_cooldown_secs is not None else COOLDOWN_SECS,
        deadline=args.test_deadline_secs if args.test_deadline_secs is not None else DEADLINE_SECS,
        pool_limit=args.test_pool_limit if args.test_pool_limit is not None else TOKEN_POOL_LIMIT,
        corpus_lines=(
            args.test_corpus_lines if args.test_corpus_lines is not None else CORPUS_LINES
        ),
        tokenizer=args.test_tokenizer,
        model=args.test_model or MODEL,
        setup_estimate=SETUP_ESTIMATE_SECS,
        setup_timeout=SETUP_TIMEOUT_SECS,
    )


def main() -> int:
    """Run the scored sweep; with --sweep, run the unscored development steps instead."""
    # Everything after the first `--` belongs to the sweep settings parser only; the task's own
    # parser never sees it, so those tokens cannot set or override its flags.
    argv = sys.argv[1:]
    sweep_tokens: list[str] | None = None
    if "--" in argv:
        split = argv.index("--")
        argv, sweep_tokens = argv[:split], argv[split + 1 :]
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--request-factory-engine")
    # vibesys-cli gives the candidate's server address in VIBESYS_SERVICE_URL.
    parser.add_argument(
        "--url", default=os.environ.get("VIBESYS_SERVICE_URL", "http://localhost:8000")
    )
    parser.add_argument("--vs-output", type=Path, help="required unless --sweep")
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="development only: unscored steps chosen by the arguments after `--`; writes no result",
    )
    parser.add_argument(
        "--check-args",
        action="store_true",
        help="with --sweep: validate the sweep settings (the arguments after `--`) only, "
        "print one JSON line, run nothing",
    )
    # Hidden smoke-test overrides; see _test_plan.
    parser.add_argument("--test-concurrencies", help=argparse.SUPPRESS)
    parser.add_argument("--test-warmup-secs", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--test-duration-secs", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--test-cooldown-secs", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--test-deadline-secs", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--test-pool-limit", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--test-corpus-lines", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--test-tokenizer", help=argparse.SUPPRESS)
    parser.add_argument("--test-model", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if sweep_tokens is not None and not args.sweep:
        parser.error("arguments after `--` are sweep settings and need --sweep")
    if args.check_args:
        if not args.sweep:
            parser.error("--check-args needs --sweep")
        return check_args(sweep_tokens)
    if args.request_factory_engine is None:
        parser.error("--request-factory-engine is required")
    if args.sweep:
        return main_dev_sweep(args, sweep_tokens)
    if args.vs_output is None:
        parser.error("--vs-output is required")
    # Hello first, so a later crash still leaves a valid stream prefix.
    write_record(args.vs_output, HELLO, first=True)
    record: dict[str, Any]
    try:
        with tempfile.TemporaryDirectory(prefix="qwen36-tracelab-scale-") as directory:
            values = sweep(args, _test_plan(args), Path(directory))
            record = {"kind": "result", "label": "", "values": values}
        status = 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        record = {"kind": "error", "message": str(exc)}
        sys.stderr.write(f"TraceLab sweep failed: {exc}\n")
        status = 1
    write_record(args.vs_output, record)
    return status


def check_args(tokens: list[str] | None) -> int:
    """Validate sweep settings only: no server, network, tokenizer, runner or GPU.

    Prints one JSON line on stdout; exit 0 if valid, 2 if not (message also on stderr).
    """
    try:
        config = resolve_sweep(tokens)
    except SweepArgsError as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": str(exc)}) + "\n")
        sys.stderr.write(f"sweep args invalid: {exc}\n")
        return 2
    sys.stdout.write(
        json.dumps({"ok": True, "args": canonical_sweep_args(config), "settings": config}) + "\n"
    )
    return 0


def main_dev_sweep(args: argparse.Namespace, tokens: list[str] | None = None) -> int:
    """Sweep mode: exit 0 only if the plan was valid and every step passed validation."""
    try:
        if any(
            getattr(args, f"test_{n}") is not None
            for n in (
                "concurrencies",
                "warmup_secs",
                "duration_secs",
                "cooldown_secs",
                "deadline_secs",
            )
        ):
            raise ValueError("--sweep takes its steps and timing after `--`, not --test-* flags")
        config = resolve_sweep(tokens)
        sys.stderr.write(f"sweep settings: {' '.join(canonical_sweep_args(config))}\n")
        plan = _test_plan(args)
        with tempfile.TemporaryDirectory(prefix="qwen36-tracelab-sweep-") as directory:
            report = dev_sweep(args, plan, config, Path(directory))
    except (OSError, ValueError, KeyError) as exc:  # SweepArgsError is a ValueError
        sys.stderr.write(f"TraceLab sweep failed: {exc}\n")
        return 1
    report_sweep(report)
    return 1 if any(step["error"] is not None for step in report["steps"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
