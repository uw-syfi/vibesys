"""The golden-logprobs gate: does the server compute the reference model's distribution?

Golden data (a JSON file of the task) holds, per prompt: its token ids, the
reference's greedy continuation (EOS ignored), and at each position of it the
reference's teacher-forced logprob of the golden token, its top-1 token and
its top-1 minus top-2 margin. Bit-exact equality is never required: a
different kernel or reduction order flips near-ties legitimately. The gate
checks that the server computes the same distribution, three ways:

1. Free-running greedy: where the server first departs from golden, the
   context was identical up to there, so the reference's margin says whether
   the departure is a near-tie.
2. Teacher-forced: the server scores prompt + golden continuation (``echo``
   with ``logprobs``); per position its logprob of the golden token and its
   argmax are compared with the reference's, with no divergence cascade.
3. Cache resume: each continuation is generated as chained rounds whose
   prompts extend the previous round's, as a session replays, so a prefix
   cache's resume path is exercised and judged by the free-running rule; and
   a streamed response must give the same ids and a correct usage chunk.

The server needs ``/v1/completions`` with the vLLM extensions ``ignore_eos``,
``return_token_ids``, ``echo`` + ``logprobs`` + ``return_tokens_as_token_ids``
and ``usage.prompt_tokens_details.cached_tokens``.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from vibesys_evaluator._private.sample import SampleError
from vibesys_evaluator.llm_serving._server import ServerError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from vibesys_evaluator.llm_serving._server import Completions

#: Chained rounds per case in the cache-resume check.
ROUNDS = 4
#: Tokens of the streamed protocol check.
STREAM_TOKENS = 16
_CASE_KEYS = ("name", "prompt_ids", "greedy_ids", "tf_logprob", "tf_top1", "tf_margin")


class GoldenError(SampleError):
    """The golden file is not usable (the message names the file, case and key)."""


@dataclass(frozen=True, kw_only=True)
class Thresholds:
    """The gate's limits (set once from calibration; never tuned to admit a candidate)."""

    near_tie_margin: float = 0.5
    max_decisive_flips: int = 0
    max_mean_abs_dlogprob: float = 0.05
    max_p99_abs_dlogprob: float = 0.25
    min_mean_prefix_fraction: float = 0.5


@dataclass(frozen=True, kw_only=True)
class Case:
    """One prompt of the golden data."""

    name: str
    prompt_ids: tuple[int, ...]
    greedy_ids: tuple[int, ...]
    tf_logprob: tuple[float, ...]
    tf_top1: tuple[int, ...]
    tf_margin: tuple[float, ...]


@dataclass(frozen=True, kw_only=True)
class Golden:
    """The golden data: the model it was made with and its cases."""

    model: str
    meta: Mapping[str, Any]
    cases: tuple[Case, ...]


def _ints(path: Path, name: str, key: str, value: object) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        msg = f"{path}: case {name!r} {key} must be a non-empty list"
        raise GoldenError(msg)
    if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in value):
        msg = f"{path}: case {name!r} {key} must hold token ids (ints >= 0)"
        raise GoldenError(msg)
    return tuple(value)


def _floats(path: Path, name: str, key: str, value: object, length: int) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != length:
        msg = f"{path}: case {name!r} {key} must be a list of {length} numbers"
        raise GoldenError(msg)
    if not all(isinstance(v, int | float) and not isinstance(v, bool) for v in value):
        msg = f"{path}: case {name!r} {key} must hold numbers"
        raise GoldenError(msg)
    return tuple(float(v) for v in value)


def _case(path: Path, index: int, raw: object) -> Case:
    if not isinstance(raw, dict):
        msg = f"{path}: cases[{index}] must be an object"
        raise GoldenError(msg)
    missing = [key for key in _CASE_KEYS if key not in raw]
    if missing:
        msg = f"{path}: cases[{index}] lacks {', '.join(missing)}"
        raise GoldenError(msg)
    name = raw["name"]
    if not isinstance(name, str) or not name:
        msg = f"{path}: cases[{index}].name must be a non-empty string"
        raise GoldenError(msg)
    greedy = _ints(path, name, "greedy_ids", raw["greedy_ids"])
    top1 = _ints(path, name, "tf_top1", raw["tf_top1"])
    if len(top1) != len(greedy):
        msg = f"{path}: case {name!r} tf_top1 must have one entry per greedy_ids token"
        raise GoldenError(msg)
    return Case(
        name=name,
        prompt_ids=_ints(path, name, "prompt_ids", raw["prompt_ids"]),
        greedy_ids=greedy,
        tf_logprob=_floats(path, name, "tf_logprob", raw["tf_logprob"], len(greedy)),
        tf_top1=top1,
        tf_margin=_floats(path, name, "tf_margin", raw["tf_margin"], len(greedy)),
    )


def load(path: Path) -> Golden:
    """Read and check the golden file."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        msg = f"golden file {path}: {error}"
        raise GoldenError(msg) from error
    if not isinstance(raw, dict):
        msg = f"{path}: the golden file must be a JSON object with meta and cases"
        raise GoldenError(msg)
    meta, cases = raw.get("meta"), raw.get("cases")
    if not isinstance(meta, dict) or not isinstance(meta.get("model"), str):
        msg = f"{path}: meta.model must name the model the golden data was made with"
        raise GoldenError(msg)
    if not isinstance(cases, list) or not cases:
        msg = f"{path}: cases must be a non-empty list"
        raise GoldenError(msg)
    parsed = tuple(_case(path, i, case) for i, case in enumerate(cases))
    names = [case.name for case in parsed]
    if len(set(names)) != len(names):
        msg = f"{path}: case names must be unique"
        raise GoldenError(msg)
    return Golden(model=meta["model"], meta=meta, cases=parsed)


@dataclass(frozen=True)
class ForcedStep:
    """The server's view of one golden position: its logprob of the golden token, its argmax."""

    logprob: float
    argmax: int


@dataclass(frozen=True)
class Completion:
    """A non-streamed greedy completion."""

    token_ids: list[int]
    cached_tokens: int | None


def _protocol(message: str) -> ServerError:
    return ServerError(f"the server's /v1/completions response {message}")


class Target:
    """The golden check's questions, asked of a server."""

    def __init__(self, server: Completions, model: str) -> None:
        """Ask ``server`` about ``model``."""
        self._server = server
        self.model = model

    def complete(self, prompt: Sequence[int], n: int) -> Completion:
        """Exactly ``n`` greedy tokens, EOS ignored, and the cached prompt tokens it reports."""
        response = self._server.complete(
            {
                "model": self.model,
                "prompt": list(prompt),
                "max_tokens": n,
                "temperature": 0,
                "ignore_eos": True,
                "return_token_ids": True,
            }
        )
        try:
            ids = response["choices"][0]["token_ids"]
        except (KeyError, IndexError, TypeError) as error:
            msg = "has no choices[0].token_ids (the server needs return_token_ids support)"
            raise _protocol(msg) from error
        if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
            msg = "choices[0].token_ids is not a list of token ids"
            raise _protocol(msg)
        usage = response.get("usage")
        details = usage.get("prompt_tokens_details") if isinstance(usage, dict) else None
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
        return Completion(list(ids), cached if isinstance(cached, int) else None)

    def teacher_forced(self, prompt: Sequence[int], cont: Sequence[int]) -> list[ForcedStep]:
        """Per position of ``cont``: the server's logprob of that token and its argmax."""
        response = self._server.complete(
            {
                "model": self.model,
                "prompt": [*prompt, *cont],
                "max_tokens": 1,
                "temperature": 0,
                "echo": True,
                "logprobs": 1,
                "return_tokens_as_token_ids": True,
            }
        )
        try:
            logprobs = response["choices"][0]["logprobs"]
            span = slice(len(prompt), len(prompt) + len(cont))
            given = logprobs["token_logprobs"][span]
            tops = logprobs["top_logprobs"][span]
            steps = []
            for value, top in zip(given, tops, strict=True):
                best = max(top.items(), key=lambda kv: kv[1])[0]
                steps.append(ForcedStep(float(value), int(best.removeprefix("token_id:"))))
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as error:
            msg = (
                "has no usable choices[0].logprobs for the echoed prompt (the server needs "
                f"echo, logprobs and return_tokens_as_token_ids): {error}"
            )
            raise _protocol(msg) from error
        if len(steps) != len(cont):
            msg = f"scored {len(steps)} of the {len(cont)} echoed continuation tokens"
            raise _protocol(msg)
        return steps

    def stream_problem(self, prompt: Sequence[int], n: int, expected: Sequence[int]) -> str | None:
        """Whether a streamed request gives the same ids and a correct usage chunk."""
        ids: list[int] = []
        usage: object = None
        done = False
        lines = self._server.stream(
            {
                "model": self.model,
                "prompt": list(prompt),
                "max_tokens": n,
                "temperature": 0,
                "ignore_eos": True,
                "return_token_ids": True,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
        )
        for line in lines:
            if not line.startswith("data: "):
                continue
            data = line.removeprefix("data: ")
            if data == "[DONE]":
                done = True
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                return f"a stream chunk is not JSON: {data[:200]!r}"
            for choice in chunk.get("choices") or []:
                ids.extend(choice.get("token_ids") or [])
            usage = chunk.get("usage") or usage
        if not done:
            return "stream did not end with data: [DONE]"
        if ids != list(expected):
            return f"streamed ids differ from non-streamed ids (first 8: {ids[:8]} vs {list(expected)[:8]})"
        if (
            not isinstance(usage, dict)
            or usage.get("completion_tokens") != n
            or usage.get("prompt_tokens") != len(prompt)
        ):
            return f"bad usage chunk: {usage}"
        return None


def common_prefix(a: Sequence[int], b: Sequence[int]) -> int:
    """The length of the longest common prefix."""
    return next(
        (i for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y), min(len(a), len(b))
    )


def p99(values: Sequence[float]) -> float:
    """The 99th percentile (nearest rank below)."""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(0.99 * len(ordered)))]


def plan_rounds(n: int, rounds: int) -> list[tuple[int, int]]:
    """``(start, length)`` of each chained round over an ``n``-token continuation."""
    per = -(-n // rounds)
    return [(start, min(per, n - start)) for start in range(0, n, per)]


def _checks(
    passes: Mapping[str, bool], summary: dict[str, Any], log: Callable[[str], None]
) -> bool:
    summary["checks"] = dict(passes)
    passed = all(passes.values())
    summary["passed"] = passed
    for name, ok in passes.items():
        log(f"  [{'ok' if ok else 'FAIL'}] {name}")
    return passed


def evaluate_base(
    target: Target, golden: Golden, th: Thresholds, log: Callable[[str], None]
) -> tuple[bool, dict[str, Any]]:
    """Free-running and teacher-forced checks over every case."""
    per_case: list[dict[str, Any]] = []
    all_dlp: list[float] = []
    non_tie: list[str] = []
    flips_total = 0
    for case in golden.cases:
        gold = case.greedy_ids
        got = target.complete(case.prompt_ids, len(gold)).token_ids
        if len(got) != len(gold):
            msg = f"{case.name}: the server returned {len(got)} tokens, expected {len(gold)}"
            raise _protocol(msg)
        prefix = common_prefix(got, gold)
        margin = case.tf_margin[prefix] if prefix < len(gold) else None
        forced = target.teacher_forced(case.prompt_ids, gold)
        dlp = [abs(f.logprob - g) for f, g in zip(forced, case.tf_logprob, strict=True)]
        flips = sum(
            1
            for f, top1, m in zip(forced, case.tf_top1, case.tf_margin, strict=True)
            if m >= th.near_tie_margin and f.argmax != top1
        )
        all_dlp += dlp
        flips_total += flips
        if margin is not None and margin >= th.near_tie_margin:
            non_tie.append(case.name)
        per_case.append(
            {
                "name": case.name,
                "prompt_tokens": len(case.prompt_ids),
                "gen_tokens": len(gold),
                "prefix_match": prefix,
                "divergence_margin": margin,
                "decisive_flips": flips,
                "mean_abs_dlogprob": statistics.fmean(dlp),
                "max_abs_dlogprob": max(dlp),
            }
        )
        where = "identical" if margin is None else f"diverge@{prefix} (golden margin {margin:.3f})"
        log(
            f"  {case.name:<15} prompt={len(case.prompt_ids):>5} {where:<34} flips={flips} "
            f"mean|dlp|={statistics.fmean(dlp):.4f} max|dlp|={max(dlp):.4f}"
        )
    summary: dict[str, Any] = {
        "identical_cases": sum(c["divergence_margin"] is None for c in per_case),
        "cases": len(per_case),
        "non_tie_divergences": non_tie,
        "decisive_flips": flips_total,
        "mean_abs_dlogprob": statistics.fmean(all_dlp),
        "p99_abs_dlogprob": p99(all_dlp),
        "max_abs_dlogprob": max(all_dlp),
        "mean_prefix_fraction": statistics.fmean(
            c["prefix_match"] / c["gen_tokens"] for c in per_case
        ),
        "per_case": per_case,
    }
    passes = {
        "free-run divergences only at near-ties": not non_tie,
        f"decisive teacher-forced flips <= {th.max_decisive_flips}": flips_total
        <= th.max_decisive_flips,
        f"mean |dlogprob| <= {th.max_mean_abs_dlogprob}": summary["mean_abs_dlogprob"]
        <= th.max_mean_abs_dlogprob,
        f"p99 |dlogprob| <= {th.max_p99_abs_dlogprob}": summary["p99_abs_dlogprob"]
        <= th.max_p99_abs_dlogprob,
        f"mean prefix fraction >= {th.min_mean_prefix_fraction}": summary["mean_prefix_fraction"]
        >= th.min_mean_prefix_fraction,
    }
    log(
        f"identical {summary['identical_cases']}/{summary['cases']}, non-tie divergences "
        f"{non_tie}, decisive flips {flips_total}, mean|dlp| {summary['mean_abs_dlogprob']:.4f}, "
        f"p99|dlp| {summary['p99_abs_dlogprob']:.4f}, max|dlp| {summary['max_abs_dlogprob']:.4f}, "
        f"mean prefix fraction {summary['mean_prefix_fraction']:.3f}"
    )
    return _checks(passes, summary, log), summary


@dataclass(frozen=True)
class RoundResult:
    """One chained round of the cache-resume check."""

    case: str
    round: int
    start: int
    context_tokens: int
    resumable_tokens: int
    cached_tokens: int | None
    matched: int
    gen_tokens: int
    divergence_margin: float | None

    def label(self) -> str:
        """``case#round@position (cached n)``."""
        return f"{self.case}#{self.round}@{self.start + self.matched} (cached {self.cached_tokens})"


def _resume_case(target: Target, case: Case) -> list[RoundResult]:
    prompt, gold, margins = list(case.prompt_ids), list(case.greedy_ids), case.tf_margin
    computed: list[int] = []  # the previous round's prompt + output[:-1]
    results = []
    for k, (start, length) in enumerate(plan_rounds(len(gold), ROUNDS), 1):
        context = prompt + gold[:start]
        completion = target.complete(context, length)
        if len(completion.token_ids) != length:
            msg = (
                f"{case.name} round {k}: the server returned {len(completion.token_ids)} tokens, "
                f"expected {length}"
            )
            raise _protocol(msg)
        matched = common_prefix(completion.token_ids, gold[start : start + length])
        results.append(
            RoundResult(
                case=case.name,
                round=k,
                start=start,
                context_tokens=len(context),
                resumable_tokens=common_prefix(computed, context),
                cached_tokens=completion.cached_tokens,
                matched=matched,
                gen_tokens=length,
                divergence_margin=margins[start + matched] if matched < length else None,
            )
        )
        computed = context + completion.token_ids[:-1]
    return results


def evaluate_resume(
    target: Target, golden: Golden, th: Thresholds, log: Callable[[str], None]
) -> tuple[bool, dict[str, Any]]:
    """The free-running rule over chained session rounds, and the cached-token report."""
    results = [r for case in golden.cases for r in _resume_case(target, case)]
    for r in results:
        where = "identical" if r.divergence_margin is None else f"diverge@{r.start + r.matched}"
        log(
            f"  {r.case:<15} round {r.round} ctx={r.context_tokens:>5} "
            f"cached={r.cached_tokens}/{r.resumable_tokens} {where}"
        )
    bad_usage = [
        r.label()
        for r in results
        if r.cached_tokens is None or not 0 <= r.cached_tokens <= r.context_tokens
    ]
    tie = th.near_tie_margin
    non_tie = [
        r.label() for r in results if r.divergence_margin is not None and r.divergence_margin >= tie
    ]
    hits = [r for r in results if r.cached_tokens]
    summary: dict[str, Any] = {
        "rounds_per_case": ROUNDS,
        "rounds": len(results),
        "cache_hit_rounds": len(hits),
        "cached_tokens": sum(r.cached_tokens or 0 for r in results),
        "resumable_tokens": sum(r.resumable_tokens for r in results),
        "non_tie_divergences": non_tie,
        "cache_hit_non_tie_divergences": [
            r.label()
            for r in hits
            if r.divergence_margin is not None and r.divergence_margin >= tie
        ],
        "bad_cached_tokens": bad_usage,
        "mean_prefix_fraction": statistics.fmean(r.matched / r.gen_tokens for r in results),
        "per_round": [asdict(r) for r in results],
    }
    floor = th.min_mean_prefix_fraction
    passes = {
        "resume: cached_tokens reported, 0 <= cached <= prompt tokens": not bad_usage,
        "resume: chained-round divergences only at near-ties": not non_tie,
        f"resume: mean round prefix fraction >= {floor}": summary["mean_prefix_fraction"] >= floor,
    }
    log(
        f"cache-hit rounds {len(hits)}/{len(results)}, cached/resumable tokens "
        f"{summary['cached_tokens']}/{summary['resumable_tokens']}, non-tie divergences {non_tie}"
    )
    if not hits:
        log("  note: the server reported no cache hits; rounds were recomputed, resume untested")
    return _checks(passes, summary, log), summary


def run_gate(
    target: Target, golden: Golden, th: Thresholds, log: Callable[[str], None]
) -> tuple[bool, dict[str, Any]]:
    """The whole gate: resume first (so it resumes from finished requests), base, stream."""
    log("cache-resume check:")
    resume_passed, resume = evaluate_resume(target, golden, th, log)
    log("base checks:")
    base_passed, summary = evaluate_base(target, golden, th, log)
    case = golden.cases[0]
    expected = target.complete(case.prompt_ids, STREAM_TOKENS).token_ids
    stream = target.stream_problem(case.prompt_ids, STREAM_TOKENS, expected)
    log(f"stream protocol check: {stream or 'ok'}")
    summary["thresholds"] = asdict(th)
    summary["resume_check"] = resume
    summary["stream_check"] = stream or "ok"
    passed = base_passed and resume_passed and stream is None
    summary["passed"] = passed
    log(f"gate: {'PASS' if passed else 'FAIL'}")
    return passed, summary


def failures(summary: Mapping[str, Any]) -> list[str]:
    """The names of the failed checks of a gate summary."""
    failed = [name for name, ok in summary.get("checks", {}).items() if not ok]
    resume = summary.get("resume_check", {})
    failed += [name for name, ok in resume.get("checks", {}).items() if not ok]
    if summary.get("stream_check", "ok") != "ok":
        failed.append(f"stream: {summary['stream_check']}")
    return failed
