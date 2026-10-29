---
name: build-client-feature
description: Add or change a feature in the TypeScript clients (the desktop app, features, and shared libraries), placing it in the package graph, writing its control state as an XState machine and its data folds as reducers, wiring the live run through @vibesys/run-session, and bounding and cancelling its asynchronous work. Use when changing client apps, features or shared libraries, scaffolding a client package, or fixing a client bug about stale results, hangs, or leaked connections.
metadata:
  kind: codebase
---

# Build a client feature

The clients are a pnpm workspace (the clients workspace): one package per concept, in
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

- Any change to a client app, a client feature, or a shared client library.
- Scaffolding or moving client packages. Changing the run protocol or the
  host-helper contract itself is the `change-wire-protocol` skill; this skill
  covers consuming them.

## Steps

1. **Place the code by layer.** Find the owning package in the package-graph
   table of
   [clients-architecture.md](../../../docs/contributing/clients-architecture.md):
   a library for shared code, a feature for one concept (its `./state` entry
   is pure, `./renderer` is DOM, `./main` is Node), and composition only in an
   app. A feature never imports another feature; an app composes cross-feature
   flows. `check_package_graph` and the dependency-cruiser rule
   `feature-state-is-pure` (with its siblings) fail on a misplaced import.
2. **Scaffold a new package, never hand-write one.** From the clients workspace root, run
   `pnpm new:package <layer> <name>` (or `pnpm new:package feature <name>`),
   then `pnpm install`; a feature starts from a machine skeleton with its
   tests. `check_package_graph` fails a hand-made package by rule name.
3. **Write control state as an XState machine, data folds as reducers.**
   Follow [references/state-and-async.md](references/state-and-async.md):
   invoked operations with ids, `after` delays on an injected clock, folds on
   `@vibesys/store` in their own modules. The Grit rule
   `features-no-hand-rolled-control` forbids timers and abort controllers in a
   feature's state and effects code, `control-state-no-store-kernel` forbids
   the reducer kernel in migrated features, and `check_package_graph` confines
   `xstate` to features and the machine packages. Migrating a feature adds it
   to the second rule's scope in the Biome configuration and drops its
   exclusion from the first.
4. **Cancel by ownership.** Every async operation takes an abort signal owned
   by whoever started it; superseding, closing, or leaving the owning state
   aborts it, and the operation disposes what it opened. Every invoke has an
   `onError`. Unenforced: whether a shell honors its signal needs a reader, and
   no lint reads XState config objects.
5. **Bound every wait** (bootstrap, dial, write, handshake, retirement) with a
   deadline from an injected `ScheduleTimeout`, so tests drive it with a fake
   clock. Inside a feature's state and effects code it is an `after`
   transition (`features-no-hand-rolled-control`). Shell code elsewhere is
   Unenforced: a timer ban there would also forbid the real scheduler that
   production injects, and a missing bound is an absence.
6. **Keep `ui/` presentational**: props in, events out; no `fetch`, sockets,
   effect hooks, timers, or transport imports. The Grit rules `ui-no-io-calls`,
   `ui-no-io-imports`, `ui-no-effect-hooks`, `ui-no-timers` and
   `web-fetch-only-in-backend` enforce it.
7. **Wire a live run through `@vibesys/run-session`.** The app builds a web
   session from `connect` (`connectRun` over its transport) and a browser
   lifecycle; read the web app's entrypoint for the working composition.
   Features render the session's types and never re-implement start-up,
   recovery or snapshot fencing. `feature-transport-is-type-only` fails a
   feature that imports transport values.
8. **Consume the protocol; do not redefine it.** Import message types from
   `@vibesys/contracts` and turn wire text into them only through its
   validators; never edit its generated bindings. The `clients_quality` check
   group regenerates the bindings and fails on a diff.
9. **Desktop: respect the runtimes.** Main-process code is a feature's
   `./main` entry or the desktop main app; a preload imports workspace
   packages only as types; welcome-view requests go through `@vibesys/ipc`'s
   parsers. `check_package_graph` fails by rule name (`preload-sandbox`,
   `runtime-reachability`), and `desktop-preloads-import-only-ipc-and-electron`
   covers preloads.
10. **Build, check, test with Turborepo** (from the clients workspace root): `pnpm check:ts`,
    `pnpm check:ts-architecture`, `pnpm check:knip`, `pnpm check:clients`,
    `pnpm test:clients`; `./scripts/check_clients.sh` runs CI's client checks together.
11. **Before opening a pull request that changes what a user sees, run the
    `verify-desktop-ui` skill**: replay fixtures through the built app, pass the
    screen checks, attach the contact sheet, judge the screens. The Run screen
    lives in `@vibesys/feature-run-page` (`./state`, `./renderer`) and is
    composed by `@vibesys/app-desktop-shell`; others edit it at the same time,
    so rebase before the final sheet. Unenforced: whether a change alters what
    a user sees needs judgment; the PR template's Screens field asks for it.

## Testing

- Test a machine with pure transitions (`getNextSnapshot`), `checkMachine`
  from `@vibesys/machine/testing` (seeded random events; every live operation
  and delay must belong to an active state), and a live actor settled in a
  chosen order for stale replies; test a fold with `checkModel` from
  `@vibesys/store/testing`. Unenforced: which cases a change needs is review
  judgment; the package scaffold only seeds them.
- Drive time and I/O with Fakes (`FakeClock`, `sessionWith` from
  `@vibesys/run-session/testing`, `FakeHelper` from
  `@vibesys/connection/testing`); never the ambient `setTimeout`
  (`check_test_isolation`, rule `wall_clock_sync`).
- The Bun test runner fails a run that collected fewer test files than the
  package holds, and bounds each test only as a hang guard. Unenforced: keep
  tests fast by driving time with a fake clock.
- App journeys get Playwright tests in each app's end-to-end suite, which CI runs.
  Unenforced: which journeys need a browser test is judgment.
- A bug fix carries a regression test that fails at the merge base.
  Unenforced: CI runs only the head; state the merge-base result in the PR.

## Known failure modes

- Unbounded waits in shell code hang a window: relay writes, page credit, process
  retirement, the gateway bootstrap, an unterminated helper line. Unenforced: step 5.
- A late reply overwrites newer state: stale chat menu results, stale thread
  creation, a superseded snapshot load. Machines make this structural
  (`features-no-hand-rolled-control`).
- Work outlives its owner: subscriptions left open at shutdown, abandoned relay
  dials, an ssh master attempt not killed on release. Unenforced: step 4.
- A gap between checking `aborted` and listening lost a cancellation; use
  `registerAbort` from `@vibesys/transport`. Unenforced: a ban on bare abort
  listeners would also hit that helper.
- A Python-side squash merge from an old branch silently reverted the
  run-session machine. `check_stale_revert` fails a change that restores an
  older copy of recently changed files; review client hunks in every PR too.
- A machine test outran the runner's per-test limit on a loaded CI runner.
  Unenforced: the bound is a runner option; keep machine tests free of real time.

## Sources of truth

- [clients-architecture.md](../../../docs/contributing/clients-architecture.md)
  (layers, runtimes, State, build) and
  [desktop-connection.md](../../../docs/contributing/desktop-connection.md).
- The package-graph checker (`check_package_graph`): graph rules and their tables.
- The clients Biome configuration: which Grit rules cover which packages, with exclusions.
- The client CI check groups; `./support/repoctl/repoctl test --dry-run` lists those a change selects.
