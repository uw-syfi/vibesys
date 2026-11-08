"""JSONSchemaBench on a Mac: the check, the benchmark and the profile load of the MLX Llama server.

check       schema validity and sentinel inclusion of CHECK_CASES answers (the gate)
benchmark   closed-loop latency of BENCHMARK_CASES requests; vibesys-cli appends --vs-output FILE
profile     a few requests, the load a profiler records; never scored

Each mode starts the candidate's server from the checkout (launch.py), asks it, and stops it.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
import workload

import launch
from vibesys_evaluator import Metric, Run, main

ROOT = Path(__file__).resolve().parents[3]

# 40 answers let the gate (95% valid) tolerate two bad ones; the benchmark's median needs fewer.
CHECK_CASES = 40
BENCHMARK_CASES = 24
PROFILE_CASES = 4
REQUEST_TIMEOUT_SECONDS = 120.0

METRICS = {
    "latency_ms.p50": Metric(unit="ms", direction="min"),
    "ttft_ms.p50": Metric(unit="ms", direction="min"),
    "tpot_ms.p50": Metric(unit="ms", direction="min"),
    "token_throughput": Metric(unit="chunks/s", direction="max"),
    "schema_ok_frac": Metric(unit="fraction", direction="max"),
}


class WorkloadError(Exception):
    """The sample is not a measurement: requests failed or the answers were wrong."""


@dataclass(frozen=True)
class Answer:
    """What one streamed request came to. A chunk is one server-sent text delta, about one token."""

    unique_id: str
    text: str
    chunks: int
    latency: float  # seconds, request sent to last chunk
    ttft: float | None  # seconds, request sent to first chunk
    error: str | None
    verdict: workload.Verdict | None


def ask(client: httpx.Client, url: str, request: workload.Case) -> Answer:
    """Send one streaming completion request and judge the answer. Never raises on a server fault."""
    body = {
        "prompt": request.prompt,
        "max_tokens": workload.MAX_TOKENS,
        "temperature": 0,
        "stream": True,
        "response_format": {"type": "json_schema", "json_schema": {"schema": request.schema}},
    }
    pieces: list[str] = []
    ttft = None
    started = time.perf_counter()
    try:
        with client.stream("POST", f"{url}/v1/completions", json=body) as response:
            if response.status_code != httpx.codes.OK:
                response.read()
                return failed(
                    request, started, f"HTTP {response.status_code}: {response.text[:200]}"
                )
            if "text/event-stream" not in response.headers.get("content-type", ""):
                return failed(request, started, "the response is not a text/event-stream")
            for line in response.iter_lines():
                if time.perf_counter() - started > REQUEST_TIMEOUT_SECONDS:
                    return failed(
                        request, started, f"no end within {REQUEST_TIMEOUT_SECONDS:.0f} s"
                    )
                if not line.startswith("data: "):
                    continue
                data = line.removeprefix("data: ").strip()
                if data == "[DONE]":
                    break
                text = delta(json.loads(data))
                if text:
                    ttft = ttft if ttft is not None else time.perf_counter() - started
                    pieces.append(text)
    except (httpx.HTTPError, ValueError) as error:
        return failed(request, started, f"{type(error).__name__}: {error}")
    latency = time.perf_counter() - started
    text = "".join(pieces)
    return Answer(
        request.unique_id, text, len(pieces), latency, ttft, None, workload.judge(request, text)
    )


def delta(event: dict[str, Any]) -> str:
    """The text an OpenAI-style completion chunk carries."""
    choices = event.get("choices") or [{}]
    return choices[0].get("text") or ""


def failed(request: workload.Case, started: float, error: str) -> Answer:
    return Answer(request.unique_id, "", 0, time.perf_counter() - started, None, error, None)


def ask_all(url: str, requests: list[workload.Case]) -> list[Answer]:
    """Ask the requests one after another: a closed loop of one."""
    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        return [ask(client, url, request) for request in requests]


def serve_and_ask(run: Run, count: int, *, warm_up: bool = False) -> list[Answer]:
    """Start the server, ask the first `count` requests of the workload, stop the server."""
    requests = workload.select(workload.load_pool(), count)
    model = workload.model_dir()
    with launch.serving(ROOT, model, run.phase("server") / "server.log") as url:
        if warm_up:
            # The first generation compiles Metal kernels; the benchmark measures the second on.
            ask_all(url, requests[:1])
        return ask_all(url, requests)


def failures(answers: list[Answer]) -> list[str]:
    return [f"{a.unique_id}: {a.error}" for a in answers if a.error is not None]


def require_completed(answers: list[Answer]) -> None:
    """Raise unless every request completed: a latency or a profile of failures means nothing."""
    if failed_requests := failures(answers):
        raise WorkloadError(
            f"{len(failed_requests)} of {len(answers)} requests failed: {failed_requests[0]}"
        )


def keep(run: Run, answers: list[Answer]) -> None:
    """Keep every answer with the job, for whoever reads why a gate failed."""
    rows = [asdict(a) for a in answers]
    (run.phase("answers") / "answers.json").write_text(json.dumps(rows, indent=2))


def check(run: Run) -> str | None:
    try:
        answers = serve_and_ask(run, CHECK_CASES)
    except launch.ServerError as error:
        return str(error)
    keep(run, answers)
    if failed_requests := failures(answers):
        return f"{len(failed_requests)} of {len(answers)} requests failed\n" + "\n".join(
            f"- {line}" for line in failed_requests[:10]
        )
    verdicts = [a.verdict for a in answers if a.verdict is not None]
    sentinels = [v.sentinel_ok for v in verdicts if v.sentinel_ok is not None]
    print(
        f"schema_valid={sum(v.valid for v in verdicts)}/{len(verdicts)} "
        f"sentinel={sum(sentinels)}/{len(sentinels)}",
        flush=True,
    )
    if reason := workload.gate(verdicts):
        bad = [f"- {a.unique_id}: {a.verdict.error}; output={a.text[:200]!r}"
               for a in answers if a.verdict and not a.verdict.valid]  # fmt: skip
        return reason + "\n" + "\n".join(bad[:10])
    print("PASS: JSON-schema accuracy gate passed", flush=True)
    return None


def benchmark(run: Run) -> None:
    try:
        answers = serve_and_ask(run, BENCHMARK_CASES, warm_up=True)
    except launch.ServerError as error:
        raise WorkloadError(str(error)) from error
    keep(run, answers)
    require_completed(answers)
    verdicts = [a.verdict for a in answers if a.verdict is not None]
    if reason := workload.gate(verdicts):
        raise WorkloadError(
            f"the answers are not correct, so their latency means nothing: {reason}"
        )
    run.report(summarize(answers))


def summarize(answers: list[Answer]) -> dict[str, float]:
    """The metrics of a sample whose requests all completed."""
    ms = 1000.0
    ttfts = [a.ttft * ms for a in answers if a.ttft is not None]
    tpots = [
        (a.latency - a.ttft) * ms / (a.chunks - 1)
        for a in answers
        if a.ttft is not None and a.chunks > 1
    ]
    wall = sum(a.latency for a in answers)  # a closed loop of one: the requests do not overlap
    return {
        "latency_ms.p50": statistics.median(a.latency * ms for a in answers),
        "ttft_ms.p50": statistics.median(ttfts),
        "tpot_ms.p50": statistics.median(tpots),
        "token_throughput": sum(a.chunks for a in answers) / wall,
        "schema_ok_frac": sum(bool(a.verdict and a.verdict.valid) for a in answers) / len(answers),
    }


def profile(run: Run) -> None:
    answers = serve_and_ask(run, PROFILE_CASES)
    keep(run, answers)
    require_completed(answers)


if __name__ == "__main__":
    main(metrics=METRICS, check=check, benchmark=benchmark, profile=profile)
