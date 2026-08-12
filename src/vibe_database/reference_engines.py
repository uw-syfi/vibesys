"""Registry of **reference engines** for the in-place superoptimization loop.

A *reference engine* names a real upstream engine whose **own source** is
vendored into the run workspace as round 0 (both the baseline and the starting
point). Each round the agent micro-optimizes that real source in place and is
graded on output-equivalence + a metric versus vanilla round 0 (see the
``examples/<engine>/OBJECTIVE.md`` for the target's contract).

``--reference-engine <name>`` is a **higher-level preset**: it supplies defaults
for the five existing target args (``--ref``/``--acc-checker``/``--bench`` and
``--domain``/``--modality``) so the whole downstream flow — which already keys
off those values — needs no new code paths. Explicit user-supplied values always
win over the preset (see ``cli._resolve_reference_engine``).

The registry deliberately carries **no** build/run/workload commands: those live
in the example's ``accuracy_checker/`` + ``benchmark/`` and in the modality
prompt, exactly like every other target.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReferenceEngine:
    """A vendored upstream engine the loop micro-optimizes in place.

    Attributes:
        name: The ``--reference-engine`` value (also the choices key).
        example_dir: Target dir (relative to the project root) holding the
            vendored ``ref_engine/`` source plus ``reference/``,
            ``accuracy_checker/``, ``benchmark/``, and ``OBJECTIVE.md``.
        default_domain: Domain pack (``--domain``) supplying the
            implementer/judge/orchestrator context for this engine.
        default_modality: Modality (``--modality``) supplying the per-task
            build/measure/grade contract for this engine.
    """

    name: str
    example_dir: str
    default_domain: str
    default_modality: str


DIFFERENTIAL_DATAFLOW = ReferenceEngine(
    name="differential-dataflow",
    example_dir="examples/differential-dataflow-cpu-bench",
    default_domain="differential-dataflow",
    default_modality="dataflow-opt",
)


REFERENCE_ENGINES: dict[str, ReferenceEngine] = {
    DIFFERENTIAL_DATAFLOW.name: DIFFERENTIAL_DATAFLOW,
}
