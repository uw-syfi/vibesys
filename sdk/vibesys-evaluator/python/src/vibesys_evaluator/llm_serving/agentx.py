"""AgentX: replay coding-agent sessions against the candidate server with the pinned harness.

``replay`` runs ``agentx-harness`` (AIPerf) at ``HARNESS_VERSION``, the one
the ``vibesys-evaluator-llm-serving`` image installs, and returns the
``METRICS`` it measured. It refuses any other installed harness version, and
an export that does not prove a clean run: an invalid submission, any error,
a unit it does not expect, or a value that is not a finite non-negative
number.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

from vibesys_evaluator._private.harness import run_harness
from vibesys_evaluator.llm_serving import _agentx
from vibesys_evaluator.llm_serving._endpoint import endpoint

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from vibesys_evaluator._private.results import Metric

__all__ = ["HARNESS_VERSION", "METRICS", "replay"]

#: The ``agentx-harness`` version every replay runs with.
HARNESS_VERSION: Final[str] = _agentx.HARNESS_VERSION
#: The metrics ``replay`` returns, with their units and directions (``main(metrics=...)``).
METRICS: Final[Mapping[str, Metric]] = _agentx.METRICS


def replay(
    *,
    model: str,
    tokenizer: str,
    tokenizer_revision: str,
    concurrency: int,
    max_context_length: int,
    artifacts: Path,
    url: str | None = None,
    mode: Literal["full", "smoke"] = "full",
    timeout_secs: float = 3300.0,
) -> dict[str, float]:
    """Replay the AgentX corpus against the server and return what it measured.

    ``full`` is the scored recipe: 15 minutes after a warmup, fixed seed and
    trajectory sampling. ``smoke`` replays two corpus entries for 60 seconds:
    a short, unscored load (a profile's, or a quick try), not held to the
    submission-validity rules.

    Args:
        model: The model name the server serves.
        tokenizer: The tokenizer to count tokens with (a Hugging Face name).
        tokenizer_revision: The tokenizer's commit (40 hex digits).
        concurrency: Sessions replayed at once.
        max_context_length: The longest context a session may reach.
        artifacts: An empty or absent directory for the harness's files (``run.phase(...)``).
        url: The server's base URL; ``endpoint()`` when None.
        mode: ``full`` or ``smoke``.
        timeout_secs: The longest the harness may run.

    Returns:
        Each metric of ``METRICS`` by name.
    """
    spec = _agentx.Replay(
        model=model,
        tokenizer=tokenizer,
        tokenizer_revision=tokenizer_revision,
        url=url if url is not None else endpoint(),
        concurrency=concurrency,
        max_context_length=max_context_length,
        mode=mode,
        timeout_secs=timeout_secs,
    )
    return _agentx.run(spec, artifacts, runner=run_harness, installed=_agentx.installed_harness())
