"""JSONSchemaBench on a Mac: the check, the benchmark and the profile load of the MLX Llama server.

check       the gate over the answers to workload.CHECK, schemas of the dev split (val)
benchmark   closed-loop latency of workload.BENCHMARK, other schemas of val; vibesys-cli appends
            --vs-output FILE
profile     a few requests, the load a profiler records; never scored
holdout     the benchmark's measurement on the held-out split (test), for a final or promoted
            commit; no manifest section runs it, so the loop never does

Each mode starts the candidate's server from the checkout (launch.py), asks it, and stops it. The
judge counts every answer's tokens itself, with the tokenizer of the pinned model snapshot, and
draws each request's sentinel from the operating system's randomness at run time.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import client
import httpx
import workload
from tokenizers import Tokenizer

import launch
from vibesys_evaluator import Metric, Run, main

ROOT = Path(__file__).resolve().parents[3]

REQUEST_TIMEOUT_SECONDS = 180.0

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
    """A request, its reply, and what the judge made of it (None when the request failed)."""

    unique_id: str
    reply: client.Reply
    tokens: int  # counted by the judge, not reported by the server
    verdict: workload.Verdict | None


def token_counter(model: Path) -> Callable[[str], int]:
    """A function that counts the tokens of a text with the pinned model's own tokenizer."""
    tokenizer = Tokenizer.from_file(str(model / "tokenizer.json"))
    return lambda text: len(tokenizer.encode(text, add_special_tokens=False).ids)


def ask(
    http: httpx.Client, url: str, request: workload.Case, count_tokens: Callable[[str], int]
) -> Answer:
    """Send one request and judge the answer."""
    reply = client.stream_completion(
        http, url, workload.request_body(request), REQUEST_TIMEOUT_SECONDS
    )
    if reply.error is not None:
        return Answer(request.unique_id, reply, 0, None)
    tokens = count_tokens(reply.text)
    return Answer(request.unique_id, reply, tokens, workload.judge(request, reply.text, tokens))


def ask_all(
    url: str, requests: list[workload.Case], count_tokens: Callable[[str], int]
) -> list[Answer]:
    """Ask the requests one after another: a closed loop of one."""
    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as http:
        return [ask(http, url, request, count_tokens) for request in requests]


def serve_and_ask(
    run: Run,
    ranks: workload.Ranks,
    *,
    split: workload.Split = workload.Split.VAL,
    warm_up: workload.Ranks | None = None,
) -> list[Answer]:
    """Start the server, ask the schemas at `ranks` of `split`, stop the server.

    With `warm_up`, one more request first, not counted: the first generation compiles Metal kernels.
    """
    pool = workload.load_pool(split)
    requests = workload.select(pool, ranks)
    model = workload.model_dir()
    count_tokens = token_counter(model)
    with launch.serving(ROOT, model, run.phase("server") / "server.log") as url:
        if warm_up is not None:
            ask_all(url, workload.select(pool, warm_up), count_tokens)
        return ask_all(url, requests, count_tokens)


def failures(answers: list[Answer]) -> list[str]:
    return [f"{a.unique_id}: {a.reply.error}" for a in answers if a.reply.error is not None]


def require_completed(answers: list[Answer]) -> None:
    """Raise unless every request completed: a latency or a profile of failures means nothing."""
    if failed_requests := failures(answers):
        raise WorkloadError(
            f"{len(failed_requests)} of {len(answers)} requests failed: {failed_requests[0]}"
        )


def verdicts_of(answers: list[Answer]) -> list[workload.Verdict]:
    return [a.verdict for a in answers if a.verdict is not None]


def counts(answers: list[Answer]) -> str:
    """The answers' outcomes in one line."""
    verdicts = verdicts_of(answers)
    held = [v.sentinel_ok for v in verdicts if v.valid and v.sentinel_ok is not None]
    by = {outcome: sum(v.outcome is outcome for v in verdicts) for outcome in workload.Outcome}
    return (
        f"answers={len(verdicts)} valid={by[workload.Outcome.VALID]} "
        f"cut_off={by[workload.Outcome.CUT_OFF]} ended_invalid={by[workload.Outcome.INVALID]} "
        f"sentinel={sum(held)}/{len(held)}"
    )


def keep(run: Run, answers: list[Answer]) -> None:
    """Keep every answer with the job, for whoever reads why a gate failed."""
    (run.phase("answers") / "answers.json").write_text(
        json.dumps([asdict(a) for a in answers], indent=2)
    )


def check(run: Run) -> str | None:
    try:
        answers = serve_and_ask(run, workload.CHECK)
    except launch.ServerError as error:
        return str(error)
    keep(run, answers)
    if failed_requests := failures(answers):
        return f"{len(failed_requests)} of {len(answers)} requests failed\n" + "\n".join(
            f"- {line}" for line in failed_requests[:10]
        )
    print(counts(answers), flush=True)
    if reason := workload.gate(verdicts_of(answers)):
        bad = [
            f"- {a.unique_id} ({a.verdict.outcome}, {a.tokens} tokens): {a.verdict.error}; "
            f"output={a.reply.text[:200]!r}"
            for a in answers
            if a.verdict and not a.verdict.valid
        ]
        return reason + "\n" + "\n".join(bad[:10])
    print("PASS: JSON-schema accuracy gate passed", flush=True)
    return None


def benchmark(run: Run) -> None:
    try:
        answers = serve_and_ask(run, workload.BENCHMARK, warm_up=workload.WARM_UP)
    except launch.ServerError as error:
        raise WorkloadError(str(error)) from error
    keep(run, answers)
    require_completed(answers)
    print(counts(answers), flush=True)
    if reason := workload.gate(verdicts_of(answers)):
        raise WorkloadError(
            f"the answers are not correct, so their latency means nothing: {reason}"
        )
    run.report(summarize(answers))


def by_source(answers: list[Answer]) -> dict[str, list[workload.Verdict]]:
    """The verdicts of the answers, grouped by the dataset subset their schema comes from."""
    groups: dict[str, list[workload.Verdict]] = {}
    for answer in answers:
        if answer.verdict is not None:
            groups.setdefault(workload.source_of(answer.unique_id), []).append(answer.verdict)
    return groups


def valid_fractions(answers: list[Answer]) -> dict[str, float]:
    """The share of valid answers per source, as the metrics `valid_frac.<source>`."""
    return {
        f"valid_frac.{source}": sum(v.valid for v in verdicts) / len(verdicts)
        for source, verdicts in sorted(by_source(answers).items())
    }


def summarize(answers: list[Answer]) -> dict[str, float]:
    """The metrics of a sample whose requests all completed."""
    ms = 1000.0
    replies = [a.reply for a in answers]
    ttfts = [r.ttft * ms for r in replies if r.ttft is not None]
    tpots = [
        (r.latency - r.ttft) * ms / (r.chunks - 1)
        for r in replies
        if r.ttft is not None and r.chunks > 1
    ]
    wall = sum(r.latency for r in replies)  # a closed loop of one: the requests do not overlap
    return {
        "latency_ms.p50": statistics.median(r.latency * ms for r in replies),
        "ttft_ms.p50": statistics.median(ttfts),
        "tpot_ms.p50": statistics.median(tpots),
        "token_throughput": sum(r.chunks for r in replies) / wall,
        "schema_ok_frac": sum(bool(a.verdict and a.verdict.valid) for a in answers) / len(answers),
    }


def profile(run: Run) -> None:
    answers = serve_and_ask(run, workload.PROFILE)
    keep(run, answers)
    require_completed(answers)


def holdout(run: Run) -> None:
    """The benchmark's measurement and gate on the held-out split, with validity by source."""
    try:
        answers = serve_and_ask(
            run, workload.HOLDOUT, split=workload.Split.TEST, warm_up=workload.HOLDOUT_WARM_UP
        )
    except launch.ServerError as error:
        raise WorkloadError(str(error)) from error
    keep(run, answers)
    require_completed(answers)
    print(counts(answers), flush=True)
    for source, verdicts in by_source(answers).items():
        by = {o: sum(v.outcome is o for v in verdicts) for o in workload.Outcome}
        print(
            f"  {source}: {len(verdicts)} answers, valid={by[workload.Outcome.VALID]} "
            f"cut_off={by[workload.Outcome.CUT_OFF]} ended_invalid={by[workload.Outcome.INVALID]}",
            flush=True,
        )
    metrics = {**summarize(answers), **valid_fractions(answers)}
    if reason := workload.gate(verdicts_of(answers)):
        # Shown to whoever reads the log, not reported: the latency of wrong answers means nothing.
        print("holdout, not reported: " + json.dumps(metrics, sort_keys=True), flush=True)
        raise WorkloadError(
            f"the answers are not correct, so their latency means nothing: {reason}"
        )
    print("holdout: " + json.dumps(metrics, sort_keys=True), flush=True)
    run.report(metrics)


def run_holdout() -> None:
    """`bench.py holdout [--vs-output FILE]`: the benchmark's sample lifecycle, another workload.

    The evaluator's command line has the three modes of `main` and no fourth, so this runs `main`
    as a benchmark whose records go to FILE, by default `holdout.jsonl` in the job's artifacts
    directory. The sources in the metrics are those of the fixed subset.
    """
    sources = {
        workload.source_of(c.unique_id)
        for c in workload.select(workload.load_pool(workload.Split.TEST), workload.HOLDOUT)
    }
    metrics = {
        **METRICS,
        **{f"valid_frac.{s}": Metric(unit="fraction", direction="max") for s in sorted(sources)},
    }
    sys.argv[1:2] = ["benchmark"]
    if "--vs-output" not in sys.argv:
        folder = Path(
            os.environ.get("VIBESYS_ARTIFACTS_DIR") or tempfile.mkdtemp(prefix="holdout-")
        )
        sys.argv += ["--vs-output", str(folder / "holdout.jsonl")]
    main(metrics=metrics, benchmark=holdout)


if __name__ == "__main__":
    if sys.argv[1:2] == ["holdout"]:
        run_holdout()
    main(metrics=METRICS, check=check, benchmark=benchmark, profile=profile)
