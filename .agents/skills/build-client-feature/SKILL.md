---
name: build-client-feature
description: Place and wire a change in the TypeScript clients (the shared libraries and the apps): the package graph, the XState machines and reducer folds of the connection libraries, and bounded, cancellable work. Desktop app work goes through the write-effect-code skill. Use when changing client apps or shared libraries, scaffolding a client package, or fixing a client bug about stale results, hangs, or leaked connections in a library.
metadata:
  kind: codebase
---

# Build a client feature

The clients are a pnpm workspace (the clients workspace): one package per concept, in
layers that depend only downward ("Layers" and "State" in
[clients-architecture.md](../../../docs/contributing/clients-architecture.md)).
**Desktop app work is Effect code** (`@vibesys/desktop`), written with the
`write-effect-code` skill. The libraries under it (`connection`, `subscription`, `logs`,
`machine`, `store`) keep their XState machines and reducer folds.

## Builds on

- `software-design` (pure core, thin shell) and `testing` (Fakes, model tests).
- `write-effect-code` for new desktop work; `write-vibesys-tests` for test tiers;
  `enforce-rules` when a fix reveals a bug class a check could close.

## When this applies

- Any change to a client app or a shared client library.
- Scaffolding or moving client packages. Changing the run protocol or the
  host-helper contract itself is the `change-wire-protocol` skill; this skill
  covers consuming them.

## Steps

1. **Place the code by layer.** Find the owning package in the package-graph
   table of
   [clients-architecture.md](../../../docs/contributing/clients-architecture.md):
   a library for shared code and composition only in an app. Inside the desktop app the folder
   rules hold the boundaries. `check_package_graph` and the dependency-cruiser rules fail a
   misplaced import.
2. **Scaffold a new package, never hand-write one.** From the clients workspace root, run
   `pnpm new:package <layer> <name>`, then `pnpm install`.
   `check_package_graph` fails a hand-made package by rule name.
3. **Control state in a library is a machine; a data fold is a reducer.** Follow
   [references/state-and-async.md](references/state-and-async.md): invoked operations with ids,
   `after` delays on an injected clock, folds on `@vibesys/store` in their own modules. New
   control state in the desktop app is an Effect service (`write-effect-code`). `check_package_graph`
   confines `xstate` to the machine packages and `effect` to the Effect desktop app.
4. **Cancel by ownership.** Every async operation takes an abort signal owned
   by whoever started it; superseding, closing, or leaving the owning state
   aborts it, and the operation disposes what it opened. Every invoke has an
   `onError`. Unenforced: whether a shell honors its signal needs a reader, and
   no lint reads XState config objects.
5. **Bound every wait** (bootstrap, dial, write, handshake, retirement) with a
   deadline from an injected `ScheduleTimeout`, so tests drive it with a fake
   clock. In a machine it is an `after` transition. Unenforced: a timer ban in shell
   code would also forbid the real scheduler that production injects, and a missing bound is
   an absence.
6. **Keep `ui/` presentational**: props in, events out; no `fetch`, sockets,
   effect hooks, timers, or transport imports. The Grit rules `ui-no-io-calls`,
   `ui-no-io-imports`, `ui-no-effect-hooks` and `ui-no-timers` enforce it.
7. **Consume the protocol; do not redefine it.** Import message types from
   `@vibesys/contracts` and turn wire text into them only through its
   validators; never edit its generated bindings. The `clients_quality` check
   group regenerates the bindings and fails on a diff.
8. **Desktop: respect the runtimes.** Main-process code lives in the app's `main/`; a preload
   imports workspace packages only as types. `check_package_graph` fails by rule name
   (`preload-sandbox`, `runtime-reachability`).
9. **Build, check, test with Turborepo** (from the clients workspace root): `pnpm check:ts`,
    `pnpm check:ts-architecture`, `pnpm check:knip`, `pnpm check:clients`,
    `pnpm test:clients`; `./scripts/check_clients.sh` runs CI's client checks together.
10. **Before opening a pull request that changes what a user sees, run the
   `verify-desktop-ui` skill**: look at the screens on the fixture page, attach the contact
   sheet, judge the screens. Unenforced: whether a change alters what
   a user sees needs judgment; the PR template's Screens field asks for it.

## Testing

- Test a machine with pure transitions (`getNextSnapshot`), `checkMachine`
  from `@vibesys/machine/testing` (seeded random events; every live operation
  and delay must belong to an active state), and a live actor settled in a
  chosen order for stale replies; test a fold with `checkModel` from
  `@vibesys/store/testing`. Unenforced: which cases a change needs is review
  judgment; the package scaffold only seeds them.
- Drive time and I/O with Fakes (`FakeClock`, `FakeHelper` from
  `@vibesys/connection/testing`); never the ambient `setTimeout`
  (`check_test_isolation`, rule `wall_clock_sync`).
- The Bun test runner fails a run that collected fewer test files than the
  package holds, and bounds each test only as a hang guard. Unenforced: keep
  tests fast by driving time with a fake clock.
- App journeys that need a browser go in the UI gallery's Playwright suite, which CI runs.
  Unenforced: which journeys need a browser test is judgment.
- A bug fix carries a regression test that fails at the merge base.
  Unenforced: CI runs only the head; state the merge-base result in the PR.

## Known failure modes

- Unbounded waits in shell code hang a window: relay writes, page credit, process
  retirement, the gateway bootstrap, an unterminated helper line. Unenforced: step 5.
- A late reply overwrites newer state: a stale listing, a superseded snapshot load. Machines
  and request identity make this structural. Unenforced: a reader checks that a reply is
  matched to its request.
- Work outlives its owner: subscriptions left open at shutdown, abandoned relay
  dials, an ssh master attempt not killed on release. Unenforced: step 4.
- A gap between checking `aborted` and listening lost a cancellation; use
  `registerAbort` from `@vibesys/transport`. Unenforced: a ban on bare abort
  listeners would also hit that helper.
- A Python-side squash merge from an old branch silently reverted a client machine.
  `check_stale_revert` fails a change that restores an
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
