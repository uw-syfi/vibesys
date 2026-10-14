---
name: build-client-feature
description: Add or change a feature in the TypeScript clients under clients/ (the web app, the desktop app, the deprecated terminal UI, features, and shared libraries), placing it in the package graph, writing its control state as an XState machine and its data folds as reducers, wiring the live run through @vibesys/run-session, and bounding and cancelling its asynchronous work. Use when editing clients/apps/*, clients/features/*, clients/libs/*, or clients/tui, scaffolding a client package, or fixing a client bug about stale results, hangs, or leaked connections.
metadata:
  kind: codebase
---

# Build a client feature

The clients are a pnpm workspace under `clients/`: one package per concept, in
layers that depend only downward ("Layers" and "State" in
[clients-architecture.md](../../../docs/contributing/clients-architecture.md)).
**Control state** is a mode with effects: it starts work, waits, retries,
times out, cancels, and decides what a late answer means (a live run session,
a host connection). A **data fold** turns ordered inputs into a value and has
no mode (the run event fold). The **shell** is the impure code around a pure
core; it owns sockets, timers and processes.

## Builds on

- `software-design` (pure core, thin shell) and `testing` (Fakes, model tests).
- `write-vibesys-tests` for test tiers; `enforce-rules` when a fix reveals a
  bug class a check could close.

## When this applies

- Any change under `clients/apps/`, `clients/features/`, `clients/libs/` or
  `clients/tui/` (the terminal UI, deprecated: it keeps building and its checks
  stay green, but new work goes to the web and desktop apps).
- Scaffolding or moving client packages. Changing the run protocol or the
  host-helper contract itself is the `change-wire-protocol` skill; this skill
  covers consuming them.

## Steps

1. **Place the code by layer.** Find the owning package in the table in
   clients-architecture.md "The package graph": a library in `clients/libs/`,
   a feature in `clients/features/<x>` (entries `./state` pure, `./renderer`
   DOM, `./main` Node), composition only in `clients/apps/`. A feature never
   imports another feature; an app composes cross-feature flows. Enforced by
   `clients/tools/scripts/check_package_graph.mjs` and
   `clients/.dependency-cruiser.mjs`.
2. **Scaffold a new package, never hand-write one.** From `clients/`, run
   `pnpm new:package <layer> <name>` (or `pnpm new:package feature <name>`),
   then `pnpm install` (`clients/tools/scripts/new_package.mjs`); a feature
   starts from a machine skeleton with its tests. A hand-made package fails
   `clients/tools/scripts/check_package_graph.mjs` by rule name.
3. **Write control state as an XState machine, data folds as reducers.**
   Follow [references/state-and-async.md](references/state-and-async.md):
   invoked operations with ids, `after` delays on an injected clock, folds on
   `@vibesys/store` in their own modules. Enforced by
   `clients/tools/lint-rules/features-no-hand-rolled-control.grit` (no timers
   or abort controllers in a feature's `state/` or `effects/`) and
   `clients/tools/lint-rules/control-state-no-store-kernel.grit`, tested in
   `clients/tools/scripts/check_control_state_rules.test.mjs`; `xstate` is
   confined by `clients/tools/scripts/check_package_graph.mjs:MACHINE_PACKAGES`.
   Migrating a feature adds it to the second rule's includes in
   `clients/biome.json` and drops its exclusion from the first.
4. **Cancel by ownership.** Every async operation takes an abort signal owned
   by whoever started it; superseding, closing, or leaving the owning state
   aborts it, and the operation disposes what it opened. Every invoke has an
   `onError`. Unenforced: whether a shell honors its signal needs a reader, and
   no lint reads XState config objects.
5. **Bound every wait** (bootstrap, dial, write, handshake, retirement) with a
   deadline from an injected `ScheduleTimeout`, so tests drive it with a fake
   clock. Inside a feature's `state/` and `effects/` it is an `after`
   transition (`clients/tools/lint-rules/features-no-hand-rolled-control.grit`).
   Shell code elsewhere is Unenforced: a timer ban there would also forbid the
   real scheduler that production injects, and a missing bound is an absence.
6. **Keep `ui/` presentational**: props in, events out; no `fetch`, sockets,
   effect hooks, timers, or transport imports (only
   `clients/apps/web/src/platform/backend.ts` calls `fetch`). Enforced by the
   Grit rules in `clients/tools/lint-rules/`, tested by
   `clients/tools/scripts/check_ui_no_effects.test.mjs` and its siblings.
7. **Wire a live run through `@vibesys/run-session`.** The app builds a
   `WebSession` from `connect` (`connectRun` over its transport) and a
   `BrowserLifecycle`, as `clients/apps/web/src/app/main.tsx` and
   `clients/apps/web/src/platform/lifecycle.ts` do. Features render the
   session's types and never re-implement start-up, recovery or snapshot
   fencing. Enforced by `clients/tools/scripts/check_package_graph.mjs` and
   `feature-transport-is-type-only` in `clients/.dependency-cruiser.mjs`. The
   TUI predates this and folds events through `@vibesys/run-state` directly.
8. **Consume the protocol; do not redefine it.** Import message types from
   `@vibesys/contracts` and turn wire text into them only through its
   validators (`clients/libs/contracts/src/protocol-parse.ts`); never edit
   `clients/libs/contracts/src/generated/`. Enforced by the `tui_quality` group
   in `.repoctl/checks.toml`, which regenerates the bindings and fails on a diff.
9. **Desktop: respect the runtimes.** Main-process code is a feature's
   `./main` or `clients/apps/desktop-main`; a preload imports workspace
   packages only as types; welcome-view requests go through `@vibesys/ipc`'s
   parsers. Enforced by `clients/tools/scripts/check_package_graph.mjs`
   (`preload-sandbox`, `runtime-reachability`).
10. **TUI**: keep OpenTUI in `ui/` and the composition root and state free of
    widgets (the `tui-*` rules in `clients/.dependency-cruiser.mjs`); for keys,
    focus and layout follow [references/tui-conventions.md](references/tui-conventions.md).
11. **Build, check, test with Turborepo** from `clients/`: `pnpm check:ts`,
    `pnpm check:ts-architecture`, `pnpm check:knip`, `pnpm check:clients`,
    `pnpm test:clients`; CI runs the `tui_*` groups in `.repoctl/checks.toml`.

## Testing

- Test a machine with pure transitions (`getNextSnapshot`), `checkMachine`
  from `@vibesys/machine/testing` (seeded random events; every live operation
  and delay must belong to an active state), and a live actor settled in a
  chosen order for stale replies; test a fold with `checkModel` from
  `@vibesys/store/testing`. Unenforced: which cases a change needs is review
  judgment; `clients/tools/scripts/new_package.mjs` only seeds them.
- Drive time and I/O with Fakes (`FakeClock`, `sessionWith` from
  `@vibesys/run-session/testing`, `FakeHelper` from
  `@vibesys/connection/testing`); never the ambient `setTimeout`
  (`scripts/check_test_isolation.py`, rule `wall_clock_sync`).
- `clients/tools/scripts/run_bun_tests.mjs` runs package tests, fails a run
  that collected fewer files than the package holds, and bounds each test only
  as a hang guard: a test that reaches it is stuck, not slow.
- TUI layout bugs get a `createTestRenderer` test at a fixed size (see
  `clients/tui/src/ui/app.test.ts`); app journeys get Playwright tests in each
  app's `e2e/`, run by the `tui_e2e` group in `.repoctl/checks.toml`.
- A bug fix carries a regression test that fails at the merge base.
  Unenforced: CI runs only the head; state the merge-base result in the PR.

## Known failure modes

- Unbounded waits in shell code hang a window: relay writes (#2289), page
  credit after EOF (#2113), process retirement (#2288), the gateway capability
  bootstrap (#1894), an unterminated helper line (#2114). Unenforced: step 5.
- A late reply overwrites newer state: stale chat menu results (#1810), stale
  thread creation (#2051), a superseded snapshot load (#2241). Machines make
  this structural (`clients/tools/lint-rules/features-no-hand-rolled-control.grit`);
  the TUI uses `clients/tui/src/latest-query-owner.ts:LatestQueryOwner`,
  Unenforced there.
- Work outlives its owner: subscriptions left open at shutdown (#2183, #1809),
  relay dials abandoned by a closed page (#2238), an ssh master attempt not
  killed on release (#2083), a handshake not aborted (#2259). Unenforced: step 4.
- A gap between checking `aborted` and listening lost a cancellation (#2284);
  use `clients/libs/transport/src/request-policy.ts:registerAbort`.
  Unenforced: a ban on bare abort listeners would also hit that helper.
- A Python-side squash merge from an old branch silently reverted the
  run-session machine (#2163, restored in #2175). Unenforced: a merge cannot
  tell a stale base from an intended revert; review `clients/` hunks in every PR.
- A machine test outran Bun's default per-test limit on a loaded CI runner
  (#2187); the bound lives in `clients/tools/scripts/run_bun_tests.mjs` only.

## Sources of truth

- [clients-architecture.md](../../../docs/contributing/clients-architecture.md)
  (layers, runtimes, State, build) and
  [desktop-connection.md](../../../docs/contributing/desktop-connection.md).
- `clients/tools/scripts/check_package_graph.mjs`: graph rules and their tables.
- `clients/biome.json`: which Grit rules cover which paths, with exclusions.
- `.repoctl/checks.toml`: the client CI groups and their commands.
