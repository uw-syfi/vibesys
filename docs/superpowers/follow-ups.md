# Follow-ups

Work found while planning or building the desktop app that belongs in its own change.

## Core-state id-less phase slots

**Limitation.** For agent executions whose events carry no `execution_id` (and no `invocation_id`), core-state's run map keeps one phase slot per agent kind and round. A second id-less execution of the same kind in the same round overwrites the first phase instead of adding one.

**Why.** `upsertPhase` (`clients/core-state/src/run-map.ts:633-648`) matches a patch without an execution id to the slot's `placeholderIndex` when it starts and to its first active index when it ends, so it never appends. `summarizePhaseSlot` (`:490-502`) records only one placeholder per slot, and `mergePhaseLists` (`:364-386`) drops a newer id-less phase whenever the older list already covers that role and round.

**Impact.** Legacy recordings without `execution_id` show fewer cards in the web app's Agents graph than the transcript shows turns (for example two implementer attempts become one card). The transcript is correct: it keys executions from the transcript entries themselves (sub-project 3, Task 3). Recordings with execution ids are unaffected.

**Fix.** Its own core-state PR, not part of the web app plans: give id-less phases an occurrence per start (as the web transcript does) in `upsertPhase`, `summarizePhaseSlot` and `mergePhaseLists`, with the TUI test gate (`pnpm --filter @vibesys/tui test`) since the TUI reads the same phases.
