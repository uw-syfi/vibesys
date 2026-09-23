"""Deterministic round artifacts shared by VibeSys's non-agent test doubles.

The stub agent client and the mock agent driver both have to answer the loop
with structured responses that are valid for the requested schema and that
tell a coherent story across rounds. That story is one thing, owned here, so
the two test doubles cannot drift into disagreeing about what a scripted run
looks like.

Payloads are keyed by response-model *name* rather than by class so this
module stays free of the schema imports the loop owns.
"""

from __future__ import annotations

import re

from vs_loop_state.api import HypothesisOutcome

# Each scripted hypothesis spans two rounds so a run exercises the
# continuation path, and every third one is disproven so the experiment log
# shows proven, rejected, and multi-round entries without a live backend.
HYPOTHESIS_ROUNDS = 2

# A rising series with a dip on the disproven hypothesis, so a scripted run
# exercises the measured column and its baseline delta rather than leaving it
# empty for want of any measurement at all.
_BASE_METRIC = 1000.0
_METRIC_STEP = 45.0
_METRIC_REGRESSION = 20.0
_METRIC_UNIT = "median_tok_per_sec"

_CLAIMS = (
    "batching the prefill step removes per-request launch overhead",
    "a larger KV cache block trades memory for fewer allocations",
    "reordering the sampler avoids a redundant device sync",
)
_ACTIONS = (
    "batch the prefill step",
    "grow the KV cache block size",
    "reorder the sampler",
)


def round_number_from_label(round_label: str | None) -> int:
    """Recover the round ordinal from a free-form round label.

    Labels are producer-formatted strings (``"round 3"``, ``"Round 3/24"``).
    A label with no digits is treated as the first round.
    """
    match = re.search(r"(\d+)", round_label or "")
    return int(match.group(1)) if match else 1


def scripted_hypothesis(round_index: int) -> tuple[str, str, str]:
    """Return the ``(id, claim, action)`` round ``round_index`` is working on."""
    index = (round_index - 1) // HYPOTHESIS_ROUNDS + 1
    position = (index - 1) % len(_CLAIMS)
    return f"H-{index:02d}", _CLAIMS[position], _ACTIONS[position]


def scripted_metric(round_index: int) -> float:
    """Return round ``round_index``'s scripted performance measurement."""
    index = (round_index - 1) // HYPOTHESIS_ROUNDS
    regressed = (index + 1) % 3 == 0
    value = _BASE_METRIC + index * _METRIC_STEP
    return value - _METRIC_REGRESSION if regressed else value


def scripted_outcome(round_index: int) -> str:
    """Return the hypothesis outcome round ``round_index`` reports."""
    index = (round_index - 1) // HYPOTHESIS_ROUNDS + 1
    if round_index % HYPOTHESIS_ROUNDS != 0:
        return HypothesisOutcome.CONTINUE.value
    return (
        HypothesisOutcome.DISPROVEN.value if index % 3 == 0 else HypothesisOutcome.SUPPORTED.value
    )


def scripted_round_payload(response_name: str, round_index: int) -> dict[str, object] | None:
    """Return a valid payload for *response_name*, or ``None`` if unscripted.

    Callers fall back to their own error handling for an unscripted schema:
    silently inventing a payload for a response model this module has never
    seen would hide a real contract change behind a passing scripted run.
    """
    hypothesis_id, claim, action = scripted_hypothesis(round_index)
    payloads: dict[str, dict[str, object]] = {
        "PreRoundDecision": {
            "need_profile": False,
            "profile_focus": "",
            "reasoning": "Scripted run skips profiling.",
        },
        "OrchestratorPlan": {
            "hypothesis_id": hypothesis_id,
            "hypothesis": claim,
            "task": action,
            "pass_criteria": "The scripted judge returns a deterministic pass.",
            "reasoning": "Scripted plan.",
            # Real runs measure on a sparse cadence, so most rounds carry no
            # verified metric. Requesting one on each closing round gives a
            # scripted run the same shape a measured run has, one number per
            # hypothesis, without waiting for the cadence to come round.
            "request_official_evaluation": round_index % HYPOTHESIS_ROUNDS == 0,
        },
        "ImplementerResponse": {
            "summary": "Scripted implementer completed without workspace changes.",
            "expected_behavior": "The run advances immediately to the judge.",
            "hypothesis_outcome": scripted_outcome(round_index),
            "perf_metric": scripted_metric(round_index),
            "perf_unit": _METRIC_UNIT,
        },
        "JudgeResponse": {
            "analysis": "Scripted judge accepted the invocation.",
            "feedback": "",
            "verdict": "pass",
        },
        "ProfilerSummary": {
            "analysis": "Scripted profile.",
            "bottlenecks": "None; no workload was executed.",
            "suggestions": "None.",
            "perf_metric": scripted_metric(round_index),
            "perf_unit": _METRIC_UNIT,
        },
        "SingleAgentRoundResponse": {
            "summary": "Scripted single-agent round completed.",
            "expected_behavior": "The lifecycle completes immediately.",
            "self_review": "Scripted review passed.",
            "feedback": "",
            "verdict": "pass",
            "bottlenecks": "None.",
            "suggestions": "None.",
            "profile_analysis": "Scripted profile.",
            "perf_metric": scripted_metric(round_index),
            "perf_unit": _METRIC_UNIT,
        },
    }
    return payloads.get(response_name)
