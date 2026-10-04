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
