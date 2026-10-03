"""A teacher-forced scoring request whose length the accuracy checker's scoring path splits.

The accuracy checker scores prompts with `echo` + `logprobs` (`HttpTarget.teacher_forced`).
The reference scores in 512-position chunks; an engine that also chunks its prefill must
handle the last, shorter chunk. r13's continuous-batching candidate failed there on the GPU
(`[512, 1]` against `[347, 248320]`), a tensor-shape bug the CPU can show as well.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from cpu_check.sessions import NEAR_TIE_MARGIN

if TYPE_CHECKING:
    from accuracy_checker.targets import ForcedStep

# 1371 scored positions: two full 512-position chunks and a 347-position remainder.
SCORE_TOKENS = 2 * 512 + 347 + 1


@dataclass(frozen=True)
class Expected:
    tokens: list[int]
    argmax: list[int]  # reference top-1 at each scored position
    margins: list[float]  # reference top-1 minus top-2 logprob at each scored position


class Reference(Protocol):
    def top2(self, tokens: list[int]) -> list[tuple[int, float]]:
        """(top-1 token, top-1 minus top-2 logprob) predicting each of tokens[1:]."""
        ...


class Target(Protocol):
    def teacher_forced(self, prompt: list[int], cont: list[int]) -> list[ForcedStep]: ...


def plan(reference: Reference, vocab_size: int, seed: int = 2) -> Expected:
    rng = random.Random(seed)
    tokens = [rng.randrange(1, vocab_size) for _ in range(SCORE_TOKENS)]
    top = reference.top2(tokens)
    return Expected(tokens, [t for t, _ in top], [m for _, m in top])


def check(target: Target, expected: Expected) -> str | None:
    """None when every scored position is returned and its top-1 matches away from near-ties."""
    try:
        steps = target.teacher_forced(expected.tokens[:1], expected.tokens[1:])
    except Exception as exc:  # any failure is the finding to report
        return f"{type(exc).__name__}: {exc}"
    if len(steps) != len(expected.argmax):
        return f"returned {len(steps)} scored positions, expected {len(expected.argmax)}"
    for i, (step, want, margin) in enumerate(
        zip(steps, expected.argmax, expected.margins, strict=True)
    ):
        if step.argmax != want and margin >= NEAR_TIE_MARGIN:
            return (
                f"top-1 at scored position {i} is token {step.argmax}, the reference's is "
                f"{want} (margin {margin:.2f} nats, not a near-tie)"
            )
    return None
