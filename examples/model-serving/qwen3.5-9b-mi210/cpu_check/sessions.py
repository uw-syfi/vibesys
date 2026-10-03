"""Chained session rounds on the tiny model: the plan, the reference answers, and the verdict.

Each session is a short coding-agent conversation, as the benchmark replays
it: round k's prompt is round k-1's prompt plus round k-1's output plus fresh
input tokens, so a prefix-caching engine resumes from the state it kept at the
end of round k-1 and must grow it (more context and more output tokens than
the request that created it). Two sessions interleave and share their first
tokens, so a cache must also handle a partial match and keep sessions apart.

Like the accuracy checker's cache-resume check, every round's prompt is built
from the reference engine's output, not the candidate's, so a near-tie flip in
one round does not cascade, and a round whose candidate output matches the
reference makes the next prompt an exact extension of what the candidate saw.
"""

from __future__ import annotations

import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from accuracy_checker.resume import common_prefix

if TYPE_CHECKING:
    from collections.abc import Sequence

    from accuracy_checker.targets import Completion

# A first divergence where the reference top-1 beats top-2 by less than this (nats) is a
# near-tie. Larger than the gate's 0.5: the tiny model's bf16 logits are ~16 nats wide,
# so their rounding step is 0.125 to 0.25 nats.
NEAR_TIE_MARGIN = 1.0
SHARED_PREFIX = 48  # tokens both sessions start with
# (fresh input tokens, max_tokens) per round. Round 2 crosses a 64-token GDN chunk
# boundary; every round asks for more output than the one before.
ROUNDS = ((40, 6), (70, 10), (30, 14))
SESSIONS = ("A", "B")
# Concurrent mode adds one session whose first prompt spans two 2048-token prefill chunks
# plus a remainder (and eight 512-token chunks plus a remainder), the longest-prompt shape
# that chunked-prefill engines split; the benchmark's prompts reach 15,733 tokens.
LONG_SESSION = "long"
LONG_FIRST_INPUT = 2 * 2048 + 347


def concurrent_sessions(count: int) -> tuple[str, ...]:
    """Names of `count` chained sessions plus the long-prompt session, for concurrent mode."""
    return (*(f"c{i:03d}" for i in range(count)), LONG_SESSION)


@dataclass(frozen=True)
class Round:
    session: str
    round: int  # 1-based
    prompt: list[int]
    expected: list[int]  # reference greedy tokens, EOS ignored
    margins: list[float]  # reference top-1 minus top-2 logprob at each expected position

    @property
    def label(self) -> str:
        return f"session {self.session} round {self.round}"


class Reference(Protocol):
    def greedy(self, prompt: list[int], n: int) -> list[int]: ...

    def margins(self, prompt: list[int], cont: list[int]) -> list[float]: ...


class Target(Protocol):
    def complete(self, prompt: list[int], n: int) -> Completion: ...


def plan(
    reference: Reference, vocab_size: int, seed: int = 0, names: Sequence[str] = SESSIONS
) -> list[Round]:
    """Interleaved rounds A1, B1, A2, B2, ... with their reference answers.

    Every session in `names` shares the first `SHARED_PREFIX` tokens; `LONG_SESSION`'s
    first round adds `LONG_FIRST_INPUT` fresh tokens instead of the usual count.
    """
    rng = random.Random(seed)

    def fresh(n: int) -> list[int]:
        return [rng.randrange(1, vocab_size) for _ in range(n)]

    shared = fresh(SHARED_PREFIX)
    context = {s: list(shared) for s in names}
    rounds = []
    for k, (n_fresh, n_out) in enumerate(ROUNDS, 1):
        for s in names:
            first_long = k == 1 and s == LONG_SESSION
            prompt = context[s] + fresh(LONG_FIRST_INPUT if first_long else n_fresh)
            expected = reference.greedy(prompt, n_out)
            rounds.append(Round(s, k, prompt, expected, reference.margins(prompt, expected)))
            context[s] = prompt + expected
    return rounds


@dataclass(frozen=True)
class Outcome:
    round: Round
    error: str | None = None  # request failed or returned a malformed response
    matched: int = 0  # leading generated tokens equal to the reference
    cached_tokens: int | None = None
    token_ids: tuple[int, ...] = ()  # the candidate's output

    @property
    def decisive_divergence(self) -> bool:
        """The candidate departed from the reference where the reference was not near a tie."""
        r = self.round
        return self.matched < len(r.expected) and r.margins[self.matched] >= NEAR_TIE_MARGIN

    @property
    def ok(self) -> bool:
        bad_usage = self.cached_tokens is None or not (
            0 <= self.cached_tokens <= len(self.round.prompt)
        )
        return self.error is None and not bad_usage and not self.decisive_divergence

    def describe(self) -> str:
        r = self.round
        head = f"{r.label} (prompt {len(r.prompt)} tokens, max_tokens {len(r.expected)})"
        if self.error is not None:
            return f"{head}: request failed: {self.error}"
        parts = [f"cached={self.cached_tokens}"]
        if self.matched == len(r.expected):
            parts.append("tokens identical")
        else:
            parts.append(
                f"diverges at output token {self.matched} "
                f"(reference margin {r.margins[self.matched]:.2f} nats)"
            )
        if self.cached_tokens is None or not 0 <= self.cached_tokens <= len(r.prompt):
            parts.append("usage.prompt_tokens_details.cached_tokens missing or out of range")
        elif self.decisive_divergence:
            parts.append(f"not a near-tie (< {NEAR_TIE_MARGIN} nats): wrong output")
        return f"{head}: {', '.join(parts)}"


def run(target: Target, rounds: list[Round]) -> list[Outcome]:
    """Send every round in order; a failed round does not stop the later ones."""
    outcomes = []
    for r in rounds:
        try:
            c = target.complete(r.prompt, len(r.expected))
        except Exception as exc:  # any failure is the finding to report
            outcomes.append(Outcome(r, error=f"{type(exc).__name__}: {exc}"))
            continue
        if len(c.token_ids) != len(r.expected):
            error = f"returned {len(c.token_ids)} tokens, expected {len(r.expected)}"
            outcomes.append(Outcome(r, error=error, cached_tokens=c.cached_tokens))
            continue
        matched = common_prefix(c.token_ids, r.expected)
        outcomes.append(
            Outcome(r, matched=matched, cached_tokens=c.cached_tokens, token_ids=tuple(c.token_ids))
        )
    return outcomes


def run_concurrent(target: Target, rounds: list[Round]) -> list[Outcome]:
    """Every session at once, each sending its rounds in order, as the benchmark replays them.

    `target` must be safe to call from several threads. Outcomes keep the order of `rounds`.
    """
    by_session: dict[str, list[Round]] = {}
    for r in rounds:
        by_session.setdefault(r.session, []).append(r)
    with ThreadPoolExecutor(max_workers=len(by_session)) as pool:
        results = list(pool.map(lambda rs: run(target, rs), by_session.values()))
    outcome_of = {(o.round.session, o.round.round): o for outs in results for o in outs}
    return [outcome_of[(r.session, r.round)] for r in rounds]
