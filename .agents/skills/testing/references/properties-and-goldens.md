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
  next to the type they generate. Draw them from the contract, not from the
  code under test (see "Generators must not share the code's assumptions").
- Assert on the public result, never on private state.
- Disable any per-example time limit the library enforces. It is a wall-clock
  dependence and a source of flakes on loaded machines.
- Keep examples cheap: no real I/O, subprocess, or network inside a property.
  For module integration, use a Fake; pure core tests need none. Keep the
  example count modest so a property runs in milliseconds to low seconds.
- When the library finds a failure, pin the failing input as an explicit
  example so the regression runs deterministically forever after, and fix the
  code.
- Two tiers run the same properties. The PR tier is derandomized and draws
  few examples, for speed. The nightly tier is randomized and draws more, and
  prints a `@reproduce_failure` line on failure; pin it with `@example(...)`.
  The per-tier counts live in the profile registration, never restated here.
- A test whose misses are costly (rare branches, expensive-to-find states)
  should pin its own `@settings(max_examples=...)`; the profile count is only a
  default for tests that do not.
- Use a plain example test for a named scenario, a documented regression, or a
  single illustrative case. Do not write a table of hand-picked inputs where a
  generator would cover the space.

## Generators must not share the code's assumptions

A property whose generator encodes the same assumption as the code under test
proves nothing: it only produces inputs the code already handles, so it passes
at the bug. Typical shapes: the code assumes records arrive sorted and the
generator sorts them; the code assumes a field is always present and the
generator always sets it; the generator builds inputs with the production
helper, so a defect in that helper is on both sides of the assertion.

- Draw inputs from the contract (the declared schema, the documented input
  space) or from captured field data, never from the implementation's helpers
  or invariants.
- List what the implementation assumes, and make the generator produce the
  opposite on purpose: unsorted, duplicated, missing, empty, out-of-range,
  reordered.
- Show the test can fail. A regression test must fail at the merge base, and a
  mutation that breaks the assumption (delete the sort, drop the default) must
  fail some test. If no test fails, the generator is too narrow.

```python
# Weak: sorts with the code's own rule, so it never sees unsorted input.
records = st.lists(records_st).map(sort_by_timestamp)
# Strong: any order the contract allows; the property is order independence.
records = st.lists(records_st).flatmap(st.permutations)
```

Unenforced: whether a generator mirrors the code's assumptions needs a reader
who knows the contract; a lint banning the module's private helpers in test
generators would only catch the helper-reuse shape and would flag legitimate
shared type builders.

## Functional core event sequences

Follow the [functional core rule](../../software-design/references/functional-core.md).
Test the core through its public API with pure properties over generated event
sequences. No async, sleeps, clocks, I/O, or Fakes belong in these tests. Supply
time and observations as event data; inspect new state and requests.

- Assert deterministic replay, input immutability, legal transitions, bounded
  accounting, and no new work after stop. Unknown outcomes never imply success.
- Enumerate small bounded state spaces and generate larger sequences. Include
  duplicate, delayed, reordered, and missing outcome events; retain minimized
  counterexamples as regressions. State the explored bounds.
- Model crash and replay at every intent boundary: before and after recording
  intent, dispatch, external acceptance, outcome recording, and completion.
  Restart from the modeled durable state, retaining external observations as
  data. Assert unfinished requests remain recoverable, retries preserve their
  identity, and each logical transition completes once.

The pure model checks recovery decisions. The interface contract suites check
actual persistence and replay safety; composed tests check shell wiring.

## Bug fixes

Write two tests: the regression at the lowest public layer that reproduces the
symptom (it must fail at the merge base), and a property that generalizes the
bug's pattern (any input of this shape, not just the reported one). Draw the
property's inputs from the contract so they include the shape that broke the
code; see "Generators must not share the code's assumptions".

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
