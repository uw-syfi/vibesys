## In-place optimization planning guidance

Choose round-sized tasks that shave measured cost from the engine while keeping
its results and guarantees **identical** to the pristine round-0 source. Every
task is an in-place micro-optimization of the engine's own code — never a
rewrite, an operator reimplementation, an algorithm or complexity-class swap, a
change to the execution or concurrency model, or a new heavyweight dependency.
Those are architectural changes and are out of scope by construction.

Good task shapes include:

- Establish a baseline first: run the accuracy checker and the benchmark on the
  vanilla source, then profile to locate the dominant hot function, allocation
  site, or loop on the measured workload.
- Remove hot-path heap allocation; reuse buffers; right-size vectors and
  small-buffer optimizations on the identified hot path.
- Tighten one existing merge, sort, scan, join, aggregation, or consolidation
  loop; cut redundant clones and copies.
- Improve data layout and locality (field ordering, alignment, SoA/AoS,
  power-of-two indexing) of existing structures — layout is fair game, the data
  *model* is not.
- Inline a hot function, add cold/hot annotations, or take a branchless path.
- Tune build and codegen flags (optimization level, LTO, codegen units, target
  CPU) that do not alter results.

Write pass criteria in terms of the objective's headline cost metric **and**
correctness: a round passes only when it is faster at output-equivalence with
the pristine reference on every workload the checker exercises and preserves the
engine's behavioral guarantees (determinism across configuration, crash/restart
recovery, race-freedom). A cost win that changes a single result or weakens a
guarantee is a failure, not a win.

Prefer one micro-optimization per round so a regression is attributable to a
single change, and so the diff against the pristine reference stays a short list
of named, defensible edits. The honest expectation for an accepted round is a
**modest percentage, not a multiple**.

If the engine's build, run, checker, or benchmark contract is uncertain, first
ask the implementer to document and validate those commands against the vanilla
source before attempting any optimization.
{% if modality is defined and modality == "dataflow_opt" %}

## Framework-owned bottleneck walk (dataflow_opt)

The framework maintains a durable **bottleneck ledger** and walks it
deterministically: it profiles the engine, ranks components by measured CPU
share, and attacks the top non-exhausted one until it plateaus (a component
exhausts after two active rounds without a ≥2% walk-metric gain), then advances
to the next. When a round names an active component, shape this round's `task` to
optimize it specifically — a **soft focus**, not a hard fence. Set
`active_component` in your output only when you have concrete evidence the walk
should revisit or reorder; the framework honors it only if it names a known,
non-exhausted component, otherwise it keeps walking the ranked list. The walk
advances only on new-hypothesis rounds, so a continuation round keeps the same
focus.
{% endif %}
