# `@vibesys/run-state`

Pure, deterministic projection of backend snapshots and ordered run events into frontend-friendly
state: the run event fold. It contains no transport, requests, terminal toolkit, theme, focus,
selection, layout, or query-result state.

`TranscriptEntry.label` and `tone` are deterministic semantic annotations derived only from event
fields. They are not terminal styling or layout decisions. A UI remains responsible for choosing
whether and how to render them.

Every reducer returns new state and performs no I/O. Selectors that depend on time require the clock
as an explicit argument.

Round identity is represented by the tagged `RoundKey` union. Known labels produce numeric keys;
unknown non-empty labels produce exact label keys, so a new backend grammar remains visible as an
unnumbered round instead of disappearing. Numeric experiment joins intentionally leave label-keyed
rounds unowned. Frontends consume the package's planned-round, outcome, experiment-ownership, phase,
and active-focus projections rather than parsing backend labels themselves.

## Consumer surface

Import only the package's entries:

| Entry | Publishes |
| --- | --- |
| `@vibesys/run-state` | `CoreState` and its read-only types, `initialCoreState`, the `reduce*` functions, `reconcileActiveExecutions`, `hasRunEnded`, `latestDiagnosticChange`, `executionStatusFor` |
| `@vibesys/run-state/rounds` | round identity (`RoundKey`), each round's agent phases and timing, planned rounds, outcomes, experiment ownership, the active focus |
| `@vibesys/run-state/performance` | the client-neutral performance series |
| `@vibesys/run-state/transcript` | the transcript model (`TranscriptEntry`, `ChatThread`) |
| `@vibesys/run-state/usage` | what usage records (`UsageRecord`, one per agent turn) add up to: `foldUsage` by role, harness, model, round or run, and `usageOverTime` in equal buckets; it reads only records, so it folds one run or many |

Replay joins, merge helpers, array indexes, and transcript fold machinery are package-private so
their representation can change without migrating a frontend. The reducer kernel that screen stores
share is `@vibesys/store`, not this package.

`CoreState` and its reachable projection types are read-only. Treat every reducer result as an
immutable snapshot: keep it, render it, or pass it to another public reducer, but never modify its
arrays, records, or entries. Development builds recursively freeze projection-owned values, so a
consumer write fails before it can corrupt another generation. Production builds omit that runtime
guard and its traversal, so correctness must not depend on the freeze.

Frontends own presentation state outside this package. The desktop app adds focus, selection, theme,
layout, local chat placeholders, transport lifecycle and React state. No
frontend imports a file below the package's entries or reproduces event-fold behavior.
