#!/usr/bin/env python3
"""Benchmark harness for the Qwen3.5-9B / 1x MI210 throughput objective.

Wraps Request Factory's `session_runner` (see ../../../resources/evaluators/
request-factory/) to replay an agentic multi-turn coding-session trace
(`text-generation-session-execution-v2`) against an OpenAI-compatible
completions server, and reports a single headline throughput number plus
supporting percentiles.

Modes:
  smoke  -- seconds. Validates plumbing only: a `session_runner --dry-run`
            static trace check (no server contact, so it cannot trip the
            prefix-cache preflight) plus a direct HTTP liveness/shape check
            against the live server. Produces no throughput number.
  quick  -- ~2-3 minutes of replay against a vLLM-class server. Small warmup
            sub-run, then a measured sub-run over a session subset.
  full   -- ~10 minutes of replay against a vLLM-class server. Same shape
            as quick, larger subset.

Prefix-cache preflight (see ../README.md "Prefix-cache preflight" section):
session_runner runs a hard, unconditional prefix-cache preflight before any
`text-generation-session-execution-v2` replay (source-verified: there is no
flag to skip it for a session-topology trace). Against a server that never
reports a genuine cache hit, `quick`/`full` will fail loudly at that gate --
this is the tool working as intended, not a bug in this harness. `smoke` mode
exists precisely so plumbing can still be validated against such a server.

Inputs: --trace/--text-file win; else $QWEN35_BENCH_ASSETS names a directory
holding `coding_session_synthetic.csv` and `corpus.txt`; else the checked-in
trace slice (traces/coding_session_0000-0259.csv, digest-checked) and the
corpus that fetch_corpus.py builds and verifies. The tokenizer comes from
--tokenizer or the Hugging Face cache ($HF_HOME, default ~/.cache/huggingface).

See ../OBJECTIVE.md and ../config/platforms/mi210.toml for the hardware facts
referenced by the defaults below.
"""

from __future__ import annotations

import argparse
import dataclasses
import glob
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Literal

# fetch_corpus.py sits next to this script; make it importable however run.py is loaded.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_corpus

Mode = Literal["smoke", "quick", "full"]

DEFAULT_MODEL = "Qwen/Qwen3.5-9B"
DEFAULT_BASE_URL = "http://127.0.0.1:8000/v1"

# Session-count knobs for each mode. Calibrated against the tuned vLLM
# baseline on 1x MI210 (see ../README.md "Modes"): quick lands at ~2-3
# minutes, full at ~10 minutes, at --max-concurrency 128. A slower candidate
# engine takes longer; these are session counts, not a wall-clock cap
# (session_runner has no deadline flag).
WARMUP_SESSIONS = 12
MODE_SESSIONS: dict[Mode, int] = {
    "smoke": 2,
    "quick": 60,
    "full": 260,
}
DEFAULT_MAX_CONCURRENCY = 128
DEFAULT_MAX_MODEL_LEN = 16384
ASSETS_ENV = "QWEN35_BENCH_ASSETS"
TRACE_FILENAME = "coding_session_synthetic.csv"
CORPUS_FILENAME = "corpus.txt"
# Sessions 0-259 of the full trace (see slice_trace.py): every session any mode replays.
DEFAULT_TRACE = Path(__file__).resolve().parent / "traces" / "coding_session_0000-0259.csv"
DEFAULT_TRACE_SHA256 = "2bca5f7911816ee758b46a626b60e8518888e9464fc4533b627052eac8180d11"
DEFAULT_TRACE_ROWS = 1513


class HarnessError(RuntimeError):
    """A domain-level failure: bad config, a missing tool, or a failed run."""


def hf_home() -> Path:
    """The Hugging Face cache root, following the `huggingface_hub` convention."""
    configured = os.environ.get("HF_HOME")
    if configured:
        return Path(configured)
    return Path.home() / ".cache" / "huggingface"


def find_tokenizer_dir(hf_home: Path, model: str) -> Path:
    """Locate the snapshot directory holding `tokenizer.json` for `model`."""
    hub_name = "models--" + model.replace("/", "--")
    matches = sorted(
        glob.glob(str(hf_home / "hub" / hub_name / "snapshots" / "*" / "tokenizer.json"))
    )
    if not matches:
        raise HarnessError(
            f"no tokenizer.json found under {hf_home}/hub/{hub_name}/snapshots/*/ "
            f"(HF_HOME={hf_home}, model={model}); pass --tokenizer explicitly."
        )
    return Path(matches[-1]).parent


@dataclasses.dataclass(frozen=True)
class SessionRunnerResult:
    returncode: int
    stdout_tail: str
    stderr_tail: str
    summary: dict[str, Any] | None


def run_session_runner(
    engine: Path,
    argv: list[str],
    *,
    timeout_s: float,
) -> SessionRunnerResult:
    """Invoke `session_runner` with `argv` and parse its `--summary-path` output.

    Translates a missing binary, a timeout, and a nonzero exit into
    `HarnessError` at the call site (the caller decides which are fatal, since
    a preflight failure and a genuine request failure warrant different
    messages); this function only reports what happened.
    """
    summary_path: Path | None = None
    if "--summary-path" in argv:
        summary_path = Path(argv[argv.index("--summary-path") + 1])

    command = [str(engine), *argv]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        raise HarnessError(f"session_runner not found at {engine}") from exc
    except subprocess.TimeoutExpired as exc:
        raise HarnessError(
            f"session_runner timed out after {timeout_s:.0f}s: {' '.join(command)}"
        ) from exc

    summary: dict[str, Any] | None = None
    if summary_path is not None and summary_path.exists():
        summary = json.loads(summary_path.read_text())

    return SessionRunnerResult(
        returncode=completed.returncode,
        stdout_tail=completed.stdout[-4000:],
        stderr_tail=completed.stderr[-4000:],
        summary=summary,
    )


def _base_replay_argv(
    *,
    trace: Path,
    text_file: Path,
    tokenizer: Path,
    base_url: str,
    model: str,
    max_concurrency: int,
    max_items: int,
    max_model_len: int,
    log_path: Path,
    summary_path: Path,
    timeline_path: Path,
) -> list[str]:
    return [
        "--trace",
        str(trace),
        "--input-file-format",
        "text-generation-session-execution-v2",
        "--text-file",
        str(text_file),
        "--tokenizer",
        str(tokenizer),
        "--token-pool-limit",
        "1000000",
        "--base-url",
        base_url,
        "--model",
        model,
        "--backend",
        "openai",
        "--temperature",
        "0",
        "--arrival-mode",
        "saturated",
        "--max-concurrency",
        str(max_concurrency),
        "--max-items",
        str(max_items),
        "--max-model-len",
        str(max_model_len),
        "--skip-when-reaching-limit",
        # Storage bandwidth must not become the measured bottleneck (see
        # ../../../resources/evaluators/request-factory/ and session_runner's
        # own --request-log / --timeline docs).
        "--request-log",
        "false",
        "--timeline",
        "false",
        "--log-path",
        str(log_path),
        "--summary-path",
        str(summary_path),
        "--timeline-path",
        str(timeline_path),
    ]


def verify_model_identity(base_url: str, model: str, timeout_s: float = 10.0) -> None:
    """Confirm `base_url` is actually serving `model`, not some other process.

    On a shared compute node a stale or unrelated process can already hold
    the port, and an HTTP status check alone cannot tell that apart from
    the intended server (observed directly: a foreign process answered
    `/health` with 404 on an already-occupied port). This checks the *body*
    of `/v1/models` for the expected model id, and is run before every mode,
    not only `smoke` -- a `quick`/`full` run against the wrong server would
    waste GPU-minutes producing a meaningless number instead of failing fast.
    """
    health_url = base_url.rsplit("/v1", 1)[0] + "/health"
    try:
        with urllib.request.urlopen(health_url, timeout=timeout_s) as response:
            if response.status >= 400:
                raise HarnessError(f"GET {health_url} returned HTTP {response.status}")
    except urllib.error.URLError as exc:
        raise HarnessError(f"GET {health_url} failed: {exc}") from exc

    models_url = f"{base_url}/models"
    try:
        with urllib.request.urlopen(models_url, timeout=timeout_s) as response:
            body = json.loads(response.read())
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise HarnessError(f"GET {models_url} failed or returned non-JSON: {exc}") from exc
    served = [entry.get("id") for entry in body.get("data", [])]
    if model not in served:
        raise HarnessError(
            f"GET {models_url} does not list {model!r} (found {served!r}); "
            f"{base_url} is very likely a different, unrelated server."
        )


def check_liveness(base_url: str, model: str, timeout_s: float = 10.0) -> None:
    """Direct HTTP checks that do not go through session_runner's preflight.

    Confirms the server is up, advertises the expected model, and answers one
    real completion -- the "correctness of plumbing" smoke checks that must
    work even against a server with no prefix-cache reporting yet.
    """
    verify_model_identity(base_url, model, timeout_s)

    payload = json.dumps(
        {"model": model, "prompt": "The capital of France is", "max_tokens": 1, "temperature": 0}
    ).encode()
    request = urllib.request.Request(
        f"{base_url}/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = json.loads(response.read())
    except urllib.error.URLError as exc:
        raise HarnessError(f"POST {base_url}/completions failed: {exc}") from exc
    if not body.get("choices"):
        raise HarnessError(f"POST {base_url}/completions returned no choices: {body}")


def run_smoke(args: argparse.Namespace, trace: Path) -> dict[str, Any]:
    check_liveness(args.base_url, args.model)

    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    result = run_session_runner(
        args.request_factory_engine,
        [
            "--trace",
            str(trace),
            "--input-file-format",
            "text-generation-session-execution-v2",
            "--dry-run",
            "--max-items",
            str(MODE_SESSIONS["smoke"]),
            "--base-url",
            args.base_url,
            "--model",
            args.model,
            "--summary-path",
            str(work_dir / "smoke_dry_run_summary.json"),
        ],
        timeout_s=30.0,
    )
    if result.returncode != 0:
        raise HarnessError(
            "smoke: session_runner --dry-run failed (trace/schema problem, not a "
            f"server problem):\n{result.stderr_tail}"
        )
    return {
        "mode": "smoke",
        "ok": True,
        "liveness_checked": [args.base_url + "/completions"],
        "dry_run_workload": (result.summary or {}).get("workload"),
    }


def measured_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    common = summary["replay"]["common"]
    cache = summary["replay"].get("prefix_cache", {})
    run_duration_s = (common.get("run_duration_ms") or 0.0) / 1000.0
    prompt_tokens = cache.get("measured_server_prompt_tokens", 0)
    output_tokens = common.get("actual_output_tokens", 0)
    total_tokens_per_s = (
        (prompt_tokens + output_tokens) / run_duration_s if run_duration_s > 0 else None
    )

    def pct(prefix: str) -> dict[str, float | None]:
        return {
            "p50": common.get(f"{prefix}_p50"),
            "p90": common.get(f"{prefix}_p90"),
            "avg": common.get(f"{prefix}_avg"),
            "max": common.get(f"{prefix}_max"),
        }

    return {
        "output_tokens_per_s": common.get("output_token_throughput_per_s"),
        "total_tokens_per_s": total_tokens_per_s,
        "request_throughput_per_s": common.get("request_throughput_per_s"),
        "ttft_ms": pct("ttft_ms"),
        "tpot_ms": pct("tpot_ms"),
        "run_duration_s": run_duration_s,
        "attempted_steps": common.get("attempted_steps"),
        "success_steps": common.get("success_steps"),
        "failed_steps": common.get("failed_steps"),
        "prefix_cache_hit_rate_server": cache.get("server_prefix_hit_rate"),
        "prefix_cache_hit_rate_planned": cache.get("planned_prefix_hit_rate"),
    }


def run_replay(args: argparse.Namespace, paths: _ResolvedPaths, mode: Mode) -> dict[str, Any]:
    verify_model_identity(args.base_url, args.model)

    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    warmup_argv = _base_replay_argv(
        trace=paths.trace,
        text_file=paths.text_file,
        tokenizer=paths.tokenizer,
        base_url=args.base_url,
        model=args.model,
        max_concurrency=min(args.max_concurrency, WARMUP_SESSIONS),
        max_items=WARMUP_SESSIONS,
        max_model_len=args.max_model_len,
        log_path=work_dir / f"{mode}_warmup_requests.jsonl",
        summary_path=work_dir / f"{mode}_warmup_summary.json",
        timeline_path=work_dir / f"{mode}_warmup_timeline.parquet",
    )
    warmup = run_session_runner(args.request_factory_engine, warmup_argv, timeout_s=180.0)
    if warmup.returncode != 0:
        raise HarnessError(
            f"{mode}: warmup sub-run failed (this is where a prefix-cache-preflight "
            f'failure surfaces -- see ../README.md "Prefix-cache preflight"):\n'
            f"{warmup.stderr_tail}"
        )

    session_count = MODE_SESSIONS[mode]
    measured_summary_path = work_dir / f"{mode}_summary.json"
    measured_argv = _base_replay_argv(
        trace=paths.trace,
        text_file=paths.text_file,
        tokenizer=paths.tokenizer,
        base_url=args.base_url,
        model=args.model,
        max_concurrency=args.max_concurrency,
        max_items=session_count,
        max_model_len=args.max_model_len,
        log_path=work_dir / f"{mode}_requests.jsonl",
        summary_path=measured_summary_path,
        timeline_path=work_dir / f"{mode}_timeline.parquet",
    )
    start = time.monotonic()
    measured = run_session_runner(
        args.request_factory_engine,
        measured_argv,
        timeout_s=args.timeout_s,
    )
    wall_s = time.monotonic() - start
    if measured.returncode != 0:
        raise HarnessError(
            f"{mode}: measured sub-run failed (session_runner exit {measured.returncode}):\n"
            f"{measured.stderr_tail}"
        )
    if measured.summary is None:
        raise HarnessError(f"{mode}: measured sub-run wrote no summary at {measured_summary_path}")

    metrics = measured_metrics(measured.summary)
    if metrics["failed_steps"]:
        raise HarnessError(
            f"{mode}: {metrics['failed_steps']} of {metrics['attempted_steps']} requests "
            f"failed during the measured window; see {work_dir / f'{mode}_requests.jsonl'} "
            "(request_log). A benchmark harness must fail loudly on request failures rather "
            "than silently discount them from throughput."
        )

    return {
        "mode": mode,
        "headline_metric": "output_tokens_per_s",
        **metrics,
        "wall_clock_s": wall_s,
        "warmup_sessions": WARMUP_SESSIONS,
        "sessions_replayed": session_count,
        "max_concurrency": args.max_concurrency,
        "base_url": args.base_url,
        "model": args.model,
        "trace": str(paths.trace),
        "engine_summary_path": str(measured_summary_path),
    }


@dataclasses.dataclass(frozen=True)
class _ResolvedPaths:
    trace: Path
    text_file: Path
    tokenizer: Path


def _configured(explicit: Path | None, flag: str, filename: str) -> Path | None:
    """The input named by its flag, else by $QWEN35_BENCH_ASSETS, else None."""
    if explicit is not None:
        path = explicit
    elif assets := os.environ.get(ASSETS_ENV):
        path = Path(assets) / filename
    else:
        return None
    if not path.is_file():
        raise HarnessError(f"{flag} does not exist: {path}")
    return path


def verify_default_trace(path: Path = DEFAULT_TRACE) -> Path:
    """Check the checked-in trace slice against its pinned digest and row count."""
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    rows = data.count(b"\n") - 1
    if digest != DEFAULT_TRACE_SHA256 or rows != DEFAULT_TRACE_ROWS:
        raise HarnessError(
            f"{path}: sha256 {digest} with {rows} rows, expected {DEFAULT_TRACE_SHA256} "
            f"with {DEFAULT_TRACE_ROWS}; regenerate it with slice_trace.py."
        )
    return path


def resolve_trace(args: argparse.Namespace) -> Path:
    return _configured(args.trace, "--trace", TRACE_FILENAME) or verify_default_trace()


def resolve_corpus(args: argparse.Namespace) -> Path:
    configured = _configured(args.text_file, "--text-file", CORPUS_FILENAME)
    if configured is not None:
        return configured
    try:
        return fetch_corpus.build(fetch_corpus.default_path())
    except fetch_corpus.CorpusError as exc:
        raise HarnessError(
            f"cannot build the default corpus ({exc}); run benchmark/fetch_corpus.py where "
            f"the network is reachable, or pass --text-file / set ${ASSETS_ENV}."
        ) from exc


def resolve_paths(args: argparse.Namespace) -> _ResolvedPaths:
    """Full resolution (trace, corpus, tokenizer), needed by quick/full.

    `smoke` mode's dry-run needs only the trace (see `resolve_trace`): it
    validates schema and static shape without loading a corpus or tokenizer,
    which is what lets it run in seconds with no server or GPU dependency.
    """
    trace = resolve_trace(args)
    text_file = resolve_corpus(args)
    tokenizer = args.tokenizer or find_tokenizer_dir(hf_home(), args.model)
    return _ResolvedPaths(trace=trace, text_file=text_file, tokenizer=tokenizer)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--mode", choices=["smoke", "quick", "full"], required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--output-json", type=Path, default=None)
    parser.add_argument(
        "--request-factory-engine",
        type=Path,
        required=True,
        help="Path to the session_runner binary (injected by the request-factory-adapter "
        "evaluator entrypoint; see ../vibesys.input.toml).",
    )
    parser.add_argument("--trace", type=Path, default=None)
    parser.add_argument("--text-file", type=Path, default=None)
    parser.add_argument("--tokenizer", type=Path, default=None)
    parser.add_argument("--max-concurrency", type=int, default=DEFAULT_MAX_CONCURRENCY)
    parser.add_argument("--max-model-len", type=int, default=DEFAULT_MAX_MODEL_LEN)
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path("/tmp/qwen3.5-9b-mi210-bench"),
        help="Scratch directory for per-run logs/timelines/summaries.",
    )
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=1800.0,
        help="Hard wall-clock timeout for the measured sub-run.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.mode == "smoke":
            result = run_smoke(args, resolve_trace(args))
        else:
            paths = resolve_paths(args)
            result = run_replay(args, paths, args.mode)
    except HarnessError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    text = json.dumps(result, indent=2)
    print(text)
    if result.get("headline_metric"):
        print(f"Primary metric: {result[result['headline_metric']]}", file=sys.stderr)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
