"""Accuracy against golden reference outputs: does the server compute the reference model?

``logprobs`` runs the gate the golden data of a task defines (reference token
ids, greedy continuations and teacher-forced logprobs, top-1 tokens and
margins per position) against the server, and returns None when it passes
or the checks that failed. It never requires bit-exact tokens: a near-tie
(reference margin below ``Thresholds.near_tie_margin``) may flip under a
different kernel. See ``docs/spec.md``, "LLM serving".
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from vibesys_evaluator._private.sample import log
from vibesys_evaluator.llm_serving import _golden
from vibesys_evaluator.llm_serving._endpoint import endpoint
from vibesys_evaluator.llm_serving._golden import Thresholds
from vibesys_evaluator.llm_serving._server import HttpCompletions

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["Thresholds", "logprobs"]


def logprobs(
    path: Path,
    *,
    url: str | None = None,
    model: str | None = None,
    thresholds: Thresholds | None = None,
    report: Path | None = None,
) -> str | None:
    """Check the server's numerics against the golden file at ``path``.

    The server needs ``/v1/completions`` with the vLLM extensions
    ``ignore_eos``, ``return_token_ids``, ``echo`` + ``logprobs`` +
    ``return_tokens_as_token_ids``, and ``usage.prompt_tokens_details``.

    Args:
        path: The golden file (JSON: ``meta`` with ``model``, and ``cases``).
        url: The server's base URL; ``endpoint()`` when None.
        model: The model name to request; the golden file's ``meta.model`` when None.
        thresholds: The gate's limits; ``Thresholds()`` when None.
        report: Where to write the full per-case report (JSON), if anywhere.

    Returns:
        None when every check passes, else the failed checks, one per line.
    """
    golden = _golden.load(path)
    target = _golden.Target(
        HttpCompletions(url if url is not None else endpoint()),
        model if model is not None else golden.model,
    )
    log(f"golden: {len(golden.cases)} cases of {golden.model} from {path.name}")
    passed, summary = _golden.run_gate(target, golden, thresholds or Thresholds(), log)
    if report is not None:
        report.write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    return None if passed else "\n".join(_golden.failures(summary))
