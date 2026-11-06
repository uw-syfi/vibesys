"""AgentX: replay coding-agent sessions against the candidate server with the pinned harness.

``replay`` runs ``agentx-harness`` (AIPerf) at ``HARNESS_VERSION``, the one
the ``vibesys-evaluator-llm-serving`` image installs, and returns the
``METRICS`` it measured. It refuses any other installed harness version, and
an export that does not prove a clean run: an invalid submission, any error,
a unit it does not expect, or a value that is not a finite non-negative
number.

``explore`` runs the same harness with any load (duration, lanes, warmup,
corpus entries, seed), for profiling and exploring in a vibesys-cli lab: it is
never a measurement and is not held to the submission-validity rules. From a
shell, ``python -m vibesys_evaluator.llm_serving.agentx explore --help``.

The ``datasets`` library cannot read even a prepared dataset from a read-only
cache, and vibesys-cli mounts operator caches read-only: a replay then copies
the prepared corpus to a temporary writable cache (removed afterwards) and
runs the harness against that.
"""

from __future__ import annotations

import os
import tempfile
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from vibesys_evaluator._private.harness import run_harness
from vibesys_evaluator.llm_serving import _agentx
from vibesys_evaluator.llm_serving._endpoint import endpoint

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vibesys_evaluator._private.results import Metric

__all__ = ["HARNESS_VERSION", "METRICS", "explore", "replay"]

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
    return _run(spec, artifacts)


def explore(
    *,
    model: str,
    tokenizer: str,
    tokenizer_revision: str,
    concurrency: int,
    max_context_length: int,
    artifacts: Path,
    duration_secs: int,
    warmup_requests_per_lane: int = 1,
    dataset_entries: int | None = None,
    seed: int = 42,
    url: str | None = None,
    timeout_secs: float = 3300.0,
) -> dict[str, float]:
    """Replay the AgentX corpus with any load and return what it measured; never a score.

    The harness, corpus and server are the scored replay's, and the load is the
    caller's: for a profiler's capture or an experiment in a vibesys-cli lab.
    Its numbers are not held to the submission-validity rules, and are no
    measurement of the task: only the task's benchmark scores.

    Args:
        model: The model name the server serves.
        tokenizer: The tokenizer to count tokens with (a Hugging Face name).
        tokenizer_revision: The tokenizer's commit (40 hex digits).
        concurrency: Sessions replayed at once (lanes).
        max_context_length: The longest context a session may reach.
        artifacts: An empty or absent directory for the harness's files.
        duration_secs: How long the measured part runs, after the warmup.
        warmup_requests_per_lane: Requests each lane sends before it.
        dataset_entries: How many corpus entries to replay; None replays the recipe's.
        seed: The harness's random seed.
        url: The server's base URL; ``endpoint()`` when None.
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
        mode="explore",
        timeout_secs=timeout_secs,
        explore=_agentx.Explore(
            duration_secs=duration_secs,
            warmup_requests_per_lane=warmup_requests_per_lane,
            dataset_entries=dataset_entries,
            seed=seed,
        ),
    )
    return _run(spec, artifacts)


def _run(spec: _agentx.Replay, artifacts: Path) -> dict[str, float]:
    installed = _agentx.installed_harness()
    _agentx.check_harness(installed)
    with (
        tempfile.TemporaryDirectory(prefix="vibesys-agentx-") as scratch,
        _agentx.harness_environment(os.environ, Path(scratch)) as env,
    ):
        return _agentx.run(
            spec, artifacts, runner=partial(run_harness, env=env), installed=installed
        )


if __name__ == "__main__":
    import sys

    from vibesys_evaluator.llm_serving._explore_cli import main

    sys.exit(main(sys.argv[1:], explore=explore))
