# Database domain prompts

Role files injected into the base prompts for `domain = "database"`. Each
`<role>.md` renders through Jinja with the uniform domain context (`modality`,
`interface`, `reference_path`, `accuracy_command`, `benchmark_command`,
`runtime_notes`, `profile_execution`, `workspace_sources`) and lands at the
`{{ domain_<role> }}` injection point of the matching base template. A missing
role file injects nothing; `single_agent.md` is derived from `implementer.md` +
`judge.md` when absent.

This domain hosts **in-place superoptimization of a real database / dataflow
engine**: the engine's own upstream source is vendored into the workspace and
micro-optimized in place, and correctness is judged as **output-equivalence with
a pristine copy of the same round-0 engine, run live** — not against a
hand-written oracle. The engine-specific workload, accuracy checker, and
benchmark live with each task under the target's `.vibesys/tasks/<task>/`; these
role files stay engine-agnostic.

Present:

- `implementer.md` — what to read and what "done" means for an in-place engine
  micro-optimization.
- `judge.md` — what to check: output-equivalence, behavioral-consistency, the
  in-place / no-rearchitecture discipline, and reward-hack guards.
- `orchestrator.md` — how the planner should pick round-sized micro-optimization
  tasks and write pass criteria in cost-metric + output-equivalence terms.
- `profiler.md` — how to capture a profile of the engine on the scored workload
  to locate the hot path without displacing the benchmark's headline metric.

The behavioral-consistency gates (determinism, crash/restart recovery,
race-freedom) are named generically here; their concrete checker wiring is added
alongside the first task's evaluator.
