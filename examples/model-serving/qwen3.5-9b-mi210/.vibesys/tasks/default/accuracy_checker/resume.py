"""Cache-resume check: the free-run gate replayed as chained session rounds.

The base checks never exercise a prefix-cache hit: teacher-forced scoring uses
`echo` + `logprobs`, which servers compute without the prefix cache, and each
free-run prompt is sent once. This check splits every golden greedy
continuation into `ROUNDS` chained requests, as a session replays: round k's
prompt is the case prompt plus the golden tokens before round k's start, and
it generates the next slice. When round k-1 matched golden, round k's prompt
is exactly round k-1's prompt plus its output, so a caching server resumes
from the state it parked at the end of round k-1 (a position not aligned to
any block or GDN chunk). Anchoring every round on golden (instead of on the
server's own output) keeps rounds after a near-tie divergence checkable.

Only HTTP against the candidate server; the policy is the base free-run policy
(README.md "Cache-resume check").
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from accuracy_checker.targets import Completion
    from accuracy_checker.thresholds import Thresholds

ROUNDS = 4


class CompletionTarget(Protocol):
    name: str

    def complete(self, prompt: list[int], n: int) -> Completion: ...


@dataclass(frozen=True)
class RoundResult:
    case: str
    round: int  # 1-based
    start: int  # golden offset of this round's first generated token
    context_tokens: int
    # Tokens of this context whose state the previous round computed (its prompt plus
    # its output minus the last, unfed token): the hit a resuming server can get.
    resumable_tokens: int
    cached_tokens: int | None  # as reported by the server
    matched: int  # leading generated tokens equal to golden
    gen_tokens: int
    divergence_margin: float | None  # golden margin at the first divergence, None if identical


def common_prefix(a: list[int], b: list[int]) -> int:
    return next(
        (i for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y), min(len(a), len(b))
    )


def plan_rounds(n: int, rounds: int) -> list[tuple[int, int]]:
    """(start, length) of each chained round over an n-token continuation."""
    per = -(-n // rounds)
    return [(start, min(per, n - start)) for start in range(0, n, per)]


def run_case(target: CompletionTarget, case: dict, rounds: int) -> list[RoundResult]:
    prompt, gold, margins = case["prompt_ids"], case["greedy_ids"], case["tf_margin"]
    computed: list[int] = []  # the previous round's prompt + output[:-1]
    results = []
    for k, (start, length) in enumerate(plan_rounds(len(gold), rounds), 1):
        ctx = prompt + gold[:start]
        c = target.complete(ctx, length)
        if len(c.token_ids) != length:
            raise RuntimeError(
                f"{case['name']} round {k}: target returned {len(c.token_ids)} tokens, "
                f"expected {length}"
            )
        matched = common_prefix(c.token_ids, gold[start : start + length])
        results.append(
            RoundResult(
                case=case["name"],
                round=k,
                start=start,
                context_tokens=len(ctx),
                resumable_tokens=common_prefix(computed, ctx),
                cached_tokens=c.cached_tokens,
                matched=matched,
                gen_tokens=length,
                divergence_margin=margins[start + matched] if matched < length else None,
            )
        )
        computed = ctx + c.token_ids[:-1]
    return results


def _label(r: RoundResult) -> str:
    return f"{r.case}#{r.round}@{r.start + r.matched} (cached {r.cached_tokens})"


def evaluate_resume(
    target: CompletionTarget, golden: dict, th: Thresholds, log=print
) -> tuple[bool, dict]:
    results = [r for case in golden["cases"] for r in run_case(target, case, ROUNDS)]
    for r in results:
        div = "identical" if r.divergence_margin is None else f"diverge@{r.start + r.matched}"
        log(
            f"  {r.case:<15} round {r.round} ctx={r.context_tokens:>5} "
            f"cached={r.cached_tokens}/{r.resumable_tokens} {div}"
        )
    bad_usage = [
        _label(r)
        for r in results
        if r.cached_tokens is None or not 0 <= r.cached_tokens <= r.context_tokens
    ]
    non_tie = [
        _label(r)
        for r in results
        if r.divergence_margin is not None and r.divergence_margin >= th.near_tie_margin
    ]
    hit_rounds = [r for r in results if r.cached_tokens]
    summary = {
        "rounds_per_case": ROUNDS,
        "rounds": len(results),
        "cache_hit_rounds": len(hit_rounds),
        "cached_tokens": sum(r.cached_tokens or 0 for r in results),
        "resumable_tokens": sum(r.resumable_tokens for r in results),
        "non_tie_divergences": non_tie,
        "cache_hit_non_tie_divergences": [
            _label(r)
            for r in hit_rounds
            if r.divergence_margin is not None and r.divergence_margin >= th.near_tie_margin
        ],
        "bad_cached_tokens": bad_usage,
        "mean_prefix_fraction": statistics.fmean(r.matched / r.gen_tokens for r in results),
        "per_round": [asdict(r) for r in results],
    }
    floor = th.min_mean_prefix_fraction
    checks = {
        "resume: cached_tokens reported, 0 <= cached <= prompt tokens": not bad_usage,
        "resume: chained-round divergences only at near-ties": not non_tie,
        f"resume: mean round prefix fraction >= {floor}": summary["mean_prefix_fraction"] >= floor,
    }
    summary["checks"] = checks
    passed = all(checks.values())
    summary["passed"] = passed
    log(
        f"cache-hit rounds {len(hit_rounds)}/{len(results)}, cached/resumable tokens "
        f"{summary['cached_tokens']}/{summary['resumable_tokens']}, non-tie divergences "
        f"{non_tie} (on cache hits: {summary['cache_hit_non_tie_divergences']})"
    )
    if not hit_rounds:
        log("  note: server reported no cache hits; rounds were recomputed, resume path untested")
    for name, ok in checks.items():
        log(f"  [{'ok' if ok else 'FAIL'}] {name}")
    return passed, summary
