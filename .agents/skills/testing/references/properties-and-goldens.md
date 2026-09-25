# Properties and golden fixtures

## Prefer properties

A property holds for every valid input, so it survives refactors and catches
cases nobody thought to write. Reach for a property-based testing library first
for anything with a large input space.

| Property | Use for |
| --- | --- |
| Round trip: `parse(serialize(x)) == x` | Typed models, protocol payloads, persisted state |
| Idempotence: `f(f(x)) == f(x)` | Normalizers, cleanup, retries |
| Invariant: something always holds | Ordering, monotone counters, conservation, "never negative" |
| Equivalence: two routes agree | Incremental vs full recompute, Fake vs real, cached vs uncached |
| Total on bad input: only documented errors | Parsers and config validators (unknown keys are always rejected) |
| Model-based: random operation sequences | Stores, caches, registries, state machines |
| Metamorphic: a change to the input has a known effect on the output | Ranking, scoring, filtering |

Guidelines:

- Build input generators from the public types, and keep reusable generators
  next to the type they generate.
- Assert on the public result, never on private state.
- Disable any per-example time limit the library enforces. It is a wall-clock
  dependence and a source of flakes on loaded machines.
- Keep examples cheap: no real I/O, subprocess, or network inside a property.
  Use a Fake. Keep the example count modest so a property runs in milliseconds
  to low seconds.
- When the library finds a failure, pin the failing input as an explicit
  example so the regression runs deterministically forever after, and fix the
  code.
- Use a plain example test for a named scenario, a documented regression, or a
  single illustrative case. Do not write a table of hand-picked inputs where a
  generator would cover the space.

## Bug fixes

Write two tests: the regression at the lowest public layer that reproduces the
symptom (it must fail at the merge base), and a property that generalizes the
bug's pattern (any input of this shape, not just the reported one).

## Golden fixtures

Use a golden fixture when behavior reduces to a deterministic snapshot of
public output: a rendered prompt, a serialized event or protocol payload, a
projection, a report.

- The snapshot must be fully deterministic. Strip or normalize timestamps, ids,
  absolute paths, and unordered collections before comparing.
- Snapshot only public output, never internal structures.
- Keep it small enough for a human to review. If nobody would read the diff,
  assert a property instead.
- Regeneration is one explicit command or flag. A failing test never rewrites
  its own fixture.
- Review the fixture diff as the behavior change it is. Do not accept a
  regenerated snapshot you have not read.
