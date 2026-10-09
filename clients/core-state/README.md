# `@vibesys/core-state`

Pure, deterministic projection of backend snapshots and ordered run events into frontend-friendly
state. It contains no transport, requests, terminal toolkit, theme, focus, selection, layout, or
query-result state.

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

Import only from `@vibesys/core-state`. The package root publishes the projection types,
`initialCoreState`, the `reduce*` functions, `reconcileActiveExecutions`, and the read helpers used
by the TUI and web client. Replay joins, merge helpers, array indexes, and transcript fold machinery
are package-private so their representation can change without migrating either frontend.

`CoreState` and its reachable projection types are read-only. Treat every reducer result as an
immutable snapshot: keep it, render it, or pass it to another public reducer, but never modify its
arrays, records, or entries. Development builds recursively freeze projection-owned values, so a
consumer write fails before it can corrupt another generation. Production builds omit that runtime
guard and its traversal, so correctness must not depend on the freeze.

Both frontends own presentation state outside this package. The TUI adds focus, selection, theme,
layout, and local chat placeholders. The web client adds transport lifecycle and React state. Neither
frontend should import a file below the package root or reproduce event-fold behavior.
